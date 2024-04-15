
import torch
import numpy as np 
from tqdm import tqdm
from fancy_einsum import einsum
import math
from functools import partial
import torch.optim
import time
import seaborn as sns
import matplotlib.pyplot as plt
import torch.nn.functional as F
import pickle
from circuit_utils import prune_dangling_edges, discretize_mask

class MaskSampler(torch.nn.Module):
    def __init__(self, pruning_cfg, complexity_mean=False):
        super().__init__()

        self.complexity_mean = complexity_mean
        self.pruning_cfg = pruning_cfg
        self.log_columns = ['complexity_loss', 'temp', 'temp_cond', 'temp_count', 'temp_reg']

        self.sampling_params = torch.nn.ParameterDict({
            k: torch.nn.ParameterList([
                torch.nn.Parameter(p_init) for p_init in pruning_cfg.init_params[k]
            ]) for k in pruning_cfg.init_params
        })

        self.fix_mask = False
        self.sampled_mask = None
        self.use_temperature = True
        self.temp_c = 0
        self.node_reg = 0
        self.def_value = 2/3
        self.sampling_function = self.sample_hard_concrete

        for param in self.parameters():
            param.register_hook(lambda grad: torch.nan_to_num(grad, nan=0, posinf=0, neginf=0))
        
    def get_sampling_params(self):
        # returns N x 2 tensor
        return torch.cat([ts.flatten(start_dim=0, end_dim=-2) if len(ts.shape) > 1 else ts.unsqueeze(0) for k in self.sampling_params for ts in self.sampling_params[k]], dim=0)
    
    def sample_hard_concrete(self, unif, sampling_params):
        # back prop against log alpha
        endpts = self.pruning_cfg.hard_concrete_endpoints
        concrete = (((.001+unif).log() - (1-unif).log() + sampling_params[...,0])/(sampling_params[...,1].relu()+.001)).sigmoid()

        hard_concrete = ((concrete + endpts[0]) * (endpts[1] - endpts[0])).clamp(0,1)

        # n_layers x (total_samples = batch_size * n_samples) x n_heads
        return hard_concrete
    
    def sample_mask(self):
        bsz = self.pruning_cfg.n_samples * self.pruning_cfg.batch_size
        prune_mask = {}
        for k in self.sampling_params:
            prune_mask[k] = []
            for ts in self.sampling_params[k]:
                # if ts.nelement() == 0:
                #     prune_mask[k].append(None)
                #     continue
                unif = torch.rand((bsz, *ts.shape[:-1])).to(self.pruning_cfg.device)
                prune_mask[k].append(self.sampling_function(unif, ts))

        self.sampled_mask = prune_mask
        
    def fix_nans(self):
        fixed = True
        with torch.no_grad():
            sampling_params = self.get_sampling_params()
            
            nancount = sampling_params.isnan().sum()

            if nancount > 0:
                print("NANs", nancount)
                for k in self.sampling_params:
                    for ts in self.sampling_params[k]:
                        ts[ts[:,1].isnan().nonzero()[:,0],-1] = self.def_value
                        if ts.isnan().sum() > 0:
                            fixed = False
        return fixed
    
    def set_temp_c(self, temp_c):
        self.temp_c = temp_c

    # beta and alpha should be same shape as x, or broadcastable
    # def f_concrete(x, beta, alpha):
    #     return ((x.log() - (1-x).log()) * beta - alpha.log()).sigmoid()

    def complexity_loss(self, sampling_params):
        return (sampling_params[...,0]-sampling_params[...,1].relu() * (math.log(-self.pruning_cfg.hard_concrete_endpoints[0]/self.pruning_cfg.hard_concrete_endpoints[1]))).sigmoid()

    def get_mask_loss(self):
        all_sampling_params = self.get_sampling_params()

        # alphas already logged
        complexity_loss = self.complexity_loss(all_sampling_params)
                    
        temperature_loss = all_sampling_params[...,1].square()

        mask_loss = self.pruning_cfg.lamb * complexity_loss.sum() + self.temp_c * temperature_loss.sum()

        with torch.no_grad():
            avg_temp = all_sampling_params[...,1].relu().mean().item()
            temp_cond = torch.nan_to_num((all_sampling_params[...,1]-1).relu().sum() / (all_sampling_params[...,1] > 1).sum(), nan=0, posinf=0, neginf=0).item() + 1
            temp_count = (2*all_sampling_params[:,1].relu().sigmoid()-1).mean().item()

            print("Complexity:", complexity_loss.sum().item(), "out of", complexity_loss.nelement())
            print("Avg temperature", avg_temp)
            print("Avg temp > 1", temp_cond)
            print("Temp count", temp_count)

        mask_details = {                
            "complexity_loss": complexity_loss.mean().item() if self.complexity_mean else complexity_loss.sum().item(),
            "temp": avg_temp,
            "temp_cond": temp_cond,
            "temp_count": temp_count,
            "temp_reg": self.temp_c
        }
        return mask_loss, mask_details
    
    def forward(self):
        if not self.fix_mask:
            self.sample_mask()
        return self.get_mask_loss() 

    def take_snapshot(self, j):
        pass

    def load_snapshot(self):
        pass

    def record_state(self, j):
        all_sampling_params = self.get_sampling_params()

        sns.histplot(torch.cat([ts.flatten() for k in self.sampled_mask for ts in self.sampled_mask[k]], dim=0).detach().flatten().cpu())
        plt.savefig(f"{self.pruning_cfg.folder}/mask{j}.png")
        plt.close()

        sns.histplot(x=all_sampling_params[:,0].sigmoid().detach().flatten().cpu(), y=all_sampling_params[:,1].detach().flatten().cpu(), bins=100)
        plt.savefig(f"{self.pruning_cfg.folder}/params-probs{j}.png")
        plt.close()

        sns.histplot(x=all_sampling_params[:,0].detach().flatten().cpu(), y=all_sampling_params[:,1].detach().flatten().cpu(), bins=100)
        plt.savefig(f"{self.pruning_cfg.folder}/params-logits{j}.png")
        plt.close()

class ConstantMaskSampler():
    def __init__(self):
        self.sampled_mask = None
        self.use_temperature = False
        self.log_columns = []

    def set_mask(self, mask):
        self.sampled_mask = mask

    def __call__(self):
        return 0, {}

    def record_state(self, j):
        pass

# for attribution patching
class AttributionPatchingMaskSampler(torch.nn.Module):
    def __init__(self, pruning_cfg):
        super().__init__()

        self.use_temperature = False
        self.log_columns = []

        n_layers = pruning_cfg.n_layers
        n_heads = pruning_cfg.n_heads
        device = pruning_cfg.device

        bsz = pruning_cfg.batch_size

        self.sampling_params = torch.nn.ParameterDict({
            "attn": torch.nn.ParameterList([
                torch.nn.Parameter(torch.ones((n_heads,)).to(device)) 
                for _ in range(n_layers)
            ]),
            "mlp": torch.nn.ParameterList([
                torch.nn.Parameter(torch.ones((1,)).to(device)) 
                for _ in range(n_layers)
            ])
        })

        self.sampled_mask = {}
        for k in self.sampling_params:
            self.sampled_mask[k] = []
            for ts in enumerate(self.sampling_params[k]):
                self.sampled_mask[k].append(torch.ones((bsz, *ts.shape)).to(device) * ts)

    def forward(self):
        return 0, {}

    def record_state(self, j):
        pass

# for direct mean ablation
class SingleComponentMaskSampler(torch.nn.Module):
    def __init__(self, pruning_cfg):
        super().__init__()

        self.use_temperature = False
        self.log_columns = []

        n_layers = pruning_cfg.n_layers
        device = pruning_cfg.device

        total_heads = n_layers * pruning_cfg.n_heads

        bsz = pruning_cfg.batch_size

        if pruning_cfg.n_samples != total_heads:
            raise Exception("In patching mode, we need to patch all heads")

        # [bsz, n_layers, n_heads]
        attn_mask = (torch.ones((total_heads, total_heads)) - torch.eye(total_heads))
        attn_mask = attn_mask.unflatten(1, (n_layers, -1)).to(device)
        self.sampling_params = {
            "attn": [
                attn_mask[:, i]
                for i in range(n_layers)
            ],
            "mlp": [
                torch.ones((total_heads,)).to(device) 
                for i in range(n_layers)
            ]
        }

        self.sampled_mask = {}
        for k in self.sampling_params:
            self.sampled_mask[k] = []
            for ts in self.sampling_params[k]:
                self.sampled_mask[k].append((torch.ones((bsz, *ts.shape)).to(device) * ts).flatten(start_dim=0, end_dim=1))

    def forward(self):
        return 0, {}

    def record_state(self, j):
        pass

# for gradient sampling
class MultiComponentMaskSampler(torch.nn.Module):
    def __init__(self, pruning_cfg, prop_sample=0.1):
        super().__init__()
        
        self.sampled_mask = None

        self.use_temperature = False
        self.log_columns = []

        self.pruning_cfg = pruning_cfg

        self.n_layers = pruning_cfg.n_layers
        self.n_heads = pruning_cfg.n_heads
        self.device = pruning_cfg.device

        self.prop_sample = prop_sample

        self.mask_perturb = torch.nn.ParameterDict({
            "attn": torch.nn.ParameterList([
                torch.nn.Parameter(torch.zeros((self.n_heads,)).to(self.device)) 
                for i in range(self.n_layers)
            ]),
            "mlp": torch.nn.ParameterList([
                torch.nn.Parameter(torch.zeros((1,)).to(self.device)) 
                for i in range(self.n_layers)
            ])
        })

    def forward(self):
        bsz = self.pruning_cfg.bsz * self.pruning_cfg.n_samples

        total_heads = self.n_layers * self.n_heads
        sampled_heads = math.ceil(self.prop_sample * total_heads)

        # select random subset
        ref_idx = torch.arange(bsz).unsqueeze(-1).repeat(1, sampled_heads)
        _, top_k_idx = torch.rand((bsz, total_heads)).topk(sampled_heads, dim=-1)

        attn_mask = torch.ones((bsz, total_heads))
        attn_mask[ref_idx.flatten(), top_k_idx.flatten()] = 0
        attn_mask = attn_mask + (1-attn_mask) * torch.rand_like(attn_mask)
        attn_mask = attn_mask.unflatten(1, (self.n_layers, -1)).to(self.device)

        fixed_mask = {
            "attn": [
                attn_mask[:, i]
                for i in range(self.n_layers)
            ],
            "mlp": [
                torch.ones((bsz,1)).to(self.device) 
                for i in range(self.n_layers)
            ]
        }

        self.sampled_mask = {}
        for k in self.mask_perturb:
            self.sampled_mask[k] = []
            for i, ts in enumerate(self.mask_perturb[k]):
                self.sampled_mask[k].append(fixed_mask[k][i] + torch.ones(bsz, *ts.shape).to(self.device) * ts)

        return 0, {}

    def record_state(self, j):
        pass