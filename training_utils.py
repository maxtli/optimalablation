# %%

import torch
from transformer_lens import HookedTransformer
from data import retrieve_owt_data
from itertools import cycle
import torch.optim
import matplotlib.pyplot as plt
import seaborn as sns

# %%
def load_model_data(model_name, batch_size=8, ctx_length=25, repeats=True, ds_name=False, device="cuda:0"):
    # device="cpu"
    device = torch.device(device if torch.cuda.is_available() else "cpu")
    model = HookedTransformer.from_pretrained(model_name, device=device)
    tokenizer = model.tokenizer
    if ds_name:
        owt_loader = retrieve_owt_data(batch_size, ctx_length, tokenizer, ds_name=ds_name)
    else:
        owt_loader = retrieve_owt_data(batch_size, ctx_length, tokenizer)
    if repeats:
        owt_iter = cycle(owt_loader)
    else:
        owt_iter = owt_loader
    return device, model, tokenizer, owt_iter

# %%

def save_hook_last_token(save_to, act, hook):
    save_to.append(act[:,-1,:])

def ablation_hook_last_token(batch_feature_idx, repl, act, hook):
    # print(act.shape, hook.name)
    # act[:,-1,:] = repl

    # act: batch_size x seq_len x activation_dim
    # repl: batch_size x features_per_batch x activation_dim
    # print(batch_feature_idx[:,0].dtype)
    # act = act.unsqueeze(1).repeat(1,features_per_batch,1,1)[batch_feature_idx[:,0],batch_feature_idx[:,1]]
    act = act[batch_feature_idx]
    # sns.histplot(torch.abs(act[:,-1]-repl).flatten().detach().cpu().numpy())
    # plt.show()
    act[:,-1] = repl
    # returns: (batch_size * features_per_batch) x seq_len x activation_dim
    # act = torch.cat([act,torch.zeros(1,act.shape[1],act.shape[2]).to(device)], dim=0)
    return act
    # return act.repeat(features_per_batch,1,1)
    # pass

def ablation_all_hook_last_token(repl, act, hook):
    # print(act.shape, hook.name)
    # act[:,-1,:] = repl

    # act: batch_size x seq_len x activation_dim
    # repl: batch_size x features_per_batch x activation_dim
    # print(batch_feature_idx[:,0].dtype)
    # act = act.unsqueeze(1).repeat(1,features_per_batch,1,1)[batch_feature_idx[:,0],batch_feature_idx[:,1]]
    # sns.histplot(torch.abs(act[:,-1]-repl).flatten().detach().cpu().numpy())
    # plt.show()
    act[:,-1] = repl
    # returns: (batch_size * features_per_batch) x seq_len x activation_dim
    # act = torch.cat([act,torch.zeros(1,act.shape[1],act.shape[2]).to(device)], dim=0)
    return act
    # return act.repeat(features_per_batch,1,1)
    # pass


class LinePlot:
    def __init___(self, stat_list):
        self.stat_list = stat_list
        self.stat_book = {x: [] for x in stat_list}
        self.t = 0
    
    def add_entry(self, entry):
        for k in self.stat_book:
            if k in entry:
                self.stat_book[k].append(entry[k])
            # default behavior is flat line
            else:
                self.stat_book[k].append(self.stat_book[k][-1])
        self.t += 1
    
    def plot(self, series, step=1, start=0, end=0):
        if end <= start:
            end = self.t
        t = [i for i in range(start, end, step)]
        for s in series:
            sns.lineplot(x=t, y=[self.stat_book[s][i] for i in range(start, end, step)])
        plt.show()
    
    def export():
        pass

