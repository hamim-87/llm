import torch 
import torch.nn as nn
from torch.nn import functional as F 
import math
from dataclasses import dataclass
import transformers
import os
import tiktoken
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"


class CausalSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0

        self.c_attn = nn.Linear(config.n_embd, 3*config.n_embd)

        self.c_proj = nn.Linear(config.n_embd, config.n_embd)
        self.c_proj.NANOGPT_SCALE_INIT = 1

        self.n_head = config.n_head
        self.n_embd = config.n_embd 

        self.register_buffer("bias", torch.tril(torch.ones(config.block_size, config.block_size))
                                    .view(1,1,config.block_size,config.block_size))
        
    def forward(self, x):
        B,T,C = x.size()

        qkv = self.c_attn(x)

        q,k,v = qkv.split(self.n_embd, dim=2)

        k = k.view(B,T,self.n_head, C//self.n_head).transpose(1,2) # (B,n_head,T,head_size) C = n_head*head_size
        q = q.view(B,T,self.n_head, C//self.n_head).transpose(1,2)
        v = v.view(B,T,self.n_head, C//self.n_head).transpose(1,2)

        att = (q @ k.transpose(-2,-1)) *(1/ math.sqrt(k.size(-1)))

        att = att.masked_fill(self.bias[:,:,:T,:T] ==0,float('-inf'))
        att = F.softmax(att, dim=-1)
        y = att @ v 
        y = y.transpose(1,2).contiguous().view(B,T,C)
        y = self.c_proj(y)
        return y   
                                                                                                                                   

class MLP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.c_fc = nn.Linear(config.n_embd, 4*config.n_embd)
        self.gelu = nn.GELU(approximate='tanh')
        self.c_proj = nn.Linear(4 * config.n_embd, config.n_embd)
        self.c_proj.NANOGPT_SCALE_INIT = 1

    def forward(self, x):
        x = self.c_fc(x)
        x = self.gelu(x)
        x = self.c_proj(x)
        return x



class Block(nn.Module):
    def __init__(self,config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)
        self.attn = CausalSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x



@dataclass
class GPTConfig:
    block_size: int = 1024
    vocab_size: int = 50257
    n_embd: int = 768
    n_head: int = 12
    n_layer: int = 12 


class GPT(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.config = config

        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size, config.n_embd),
            wpe = nn.Embedding(config.block_size,config.n_embd),
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            ln_f = nn.LayerNorm(config.n_embd)
        ))
        self.lm_head = nn.Linear(config.n_embd,config.vocab_size,bias=False)

        # weight sharing scheme
        self.transformer.wte.weight = self.lm_head.weight


        #applying initial weights

        self.apply(self._init_weights)

    def _init_weights(self,module):
        if isinstance(module, nn.Linear):
            std = 0.02 
            if hasattr(module, 'NANOGPT_SCALE_INIT'):
                std = (2*self.config.n_layer) ** -0.5
            torch.nn.init.normal_(module.weight, mean= 0.0,std=std)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight,mean=0.0,std=0.02)  


    def forward(self, idx,targets = None):
        B,T = idx.size()
        assert T <= self.config.block_size, f"Cannot forward sequence of length {T}, block size is only {self.config.block_size}"

        pos = torch.arange(0,T,dtype=torch.long,device=idx.device) #(T)
        pos_emb = self.transformer.wpe(pos) #(T,n_embd)
        tok_emb = self.transformer.wte (idx) #(B,T,n_embd)

        x = tok_emb + pos_emb

        for block in self.transformer.h:
            x = block(x)
        
        x = self.transformer.ln_f(x)

        logits = self.lm_head(x)

        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1,logits.size(-1)), targets.view(-1))

        return logits,loss



    @classmethod
    def from_pretrained(cls, model_type):
        """Loads pretrained GPT-2 model weights from huggingface"""
        assert model_type in {'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl'}
        from transformers import GPT2LMHeadModel
        print("loading weights from pretrained gpt: %s" % model_type)

        # n_layer, n_head and n_embd are determined from model_type
        config_args = {
            'gpt2':         dict(n_layer=12, n_head=12, n_embd=768),  # 124M params
            'gpt2-medium':  dict(n_layer=24, n_head=16, n_embd=1024), # 350M params
            'gpt2-large':   dict(n_layer=36, n_head=20, n_embd=1280), # 774M params
            'gpt2-xl':      dict(n_layer=48, n_head=25, n_embd=1600), # 1558M params
        }[model_type]
        config_args['vocab_size'] = 50257 # always 50257 for GPT model checkpoints
        config_args['block_size'] = 1024 # always 1024 for GPT model checkpoints
        # create a from-scratch initialized minGPT model
        config = GPTConfig(**config_args)
        model = GPT(config)
        sd = model.state_dict()
        sd_keys = sd.keys()
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')] # discard this mask / buffer, not a param

        # init a huggingface/transformers model
        model_hf = GPT2LMHeadModel.from_pretrained(model_type)
        sd_hf = model_hf.state_dict()

        # copy while ensuring all of the parameters are aligned and match in names and shapes
        sd_keys_hf = sd_hf.keys()
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')] # ignore these, just a buffer
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')] # same, just the mask (buffer)
        transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']
        # basically the openai checkpoints use a "Conv1D" module, but we only want to use a vanilla Linear
        # this means that we have to transpose these weights when we import them
        assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
        for k in sd_keys_hf:
            if any(k.endswith(w) for w in transposed):
                # special treatment for the Conv1D weights we need to transpose
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k].t())
            else:
                # vanilla copy over the other parameters
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])

        return model
    

#--------------------------------------------------------------------------------------


#device check 
if torch.cuda.is_available():
    print("using device cuda")
else: 
    print("using device cpu")

#inintialize the model with gpt2 weights 
# model = GPT.from_pretrained('gpt2')
# print('hehe gpt2')


#initialize the model wiht random value
model = GPT(GPTConfig())
print("hehe random")

# model layer checking
# sd = model.state_dict()
# for k,v in sd.items():
#   print(k,v.shape)

# sanity check 
# print(sd['transformer.wpe.weight'][1][:20])


device = 'cuda' if torch.cuda.is_available() else 'cpu'

# model.eval()
model.to(device)


#encode 
enc = tiktoken.get_encoding('gpt2')


# TRAINING PERPOSE 

class DataLoader:
    def __init__(self,B,T):
        self.B = B
        self.T = T

        with open('input.txt', 'r') as f:
            text = f.read()
        tokens = enc.encode(text)
        self.tokens = torch.tensor(tokens)

        self.current_pos = 0

    def next_batch(self):
        B,T = self.B, self.T

        buf = self.tokens[self.current_pos:self.current_pos+B*T+1]
       

        x = buf[:-1].view(B,T)
        y = buf[1:].view(B,T)

        self.current_pos += B*T+1

        if self.current_pos+B*T+1> len(self.tokens):
            self.current_pos = 0
        
        return x,y


        


trian_loader = DataLoader(4,32)

optimizer = torch.optim.AdamW(model.parameters(), lr = 3e-4)

for i in range(50):
    x,y = trian_loader.next_batch()
    x,y = x.to(device),y.to(device)

    optimizer.zero_grad()
    with torch.autocast(device_type=device, dtype=torch.bfloat16):
        logits,loss = model(x,y)
    loss.backward()
    optimizer.step()


    print(f"step {i}: loss: {loss}")

















#generate text constant
num_return_sequences = 5
max_length = 30


# creating the input x



tokens = enc.encode("Hello, I'm a language model, ")
tokens = torch.tensor(tokens, dtype= torch.long)
tokens = tokens.unsqueeze(0).repeat(num_return_sequences,1)

x = tokens.to(device)


def generate(x,max_length, num_return_sequences):
    
    while x.size(1) < max_length:

        with torch.no_grad():
            logits,loss = model(x) #(B,T,vocab_size)

            logits = logits[:,-1,:] #(B,vocab_size) 

            probs = F.softmax(logits, dim=-1)

            topk_probs, topk_indices = torch.topk(probs,50,dim=-1)

            ix = torch.multinomial(topk_probs, 1)

            xcol = torch.gather(topk_indices,-1,ix)

            x = torch.cat((x,xcol),dim=1)
           


    for i in range(num_return_sequences):
        tokens = x[i,:max_length].tolist()
        decode = enc.decode(tokens)
        print(">>", decode)

generate(x,max_length,num_return_sequences)


