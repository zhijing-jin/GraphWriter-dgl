import torch
import dgl
import numpy as np
import torch.utils.data
import json
import pickle
import random
from collections import namedtuple
config_type = namedtuple('Config', ['nhid', 'nhead', 'head_dim', 'weight_decay', 'prop', 'title', 'test', 'batch_size', 'beam_size', 'epoch', 'beam_max_len', 'enc_lstm_layers', 'seed', 'lr', 'clip', 'emb_drop', 'attn_drop', 'drop', 'lp', 'graph_enc', 'train_file', 'valid_file', 'test_file', 'save_dataset', 'save_model', 'gpu'])
NODE_TYPE = {'entity': 0, 'root': 1, 'relation':2}

def get_default_config():
    config = config_type(nhid=500, nhead=4, head_dim=125, weight_decay=0.0, prop=2, title=True, test=False, batch_size=32, beam_size=1, epoch=20, beam_max_len=200, enc_lstm_layers=2, seed=0, lr=1e-1, clip=1.0, emb_drop=0.0, attn_drop=0.1, drop=0.1, lp=1.0, graph_enc='gtrans', train_file='data/agenda/train.json', valid_file='data/agenda/valid.json', test_file='data/agenda/test.json', save_dataset='data.pickle', save_model='saved_model.pt', gpu=0)
    config.dec_ninp = config.nhid * 3 if config.title else config.nhid * 2
    return config

def write_txt(batch, seqs, text_vocab):
    # converting the prediction to real text.
    ret = []
    for b, seq in enumerate(seqs):
        txt = []
        for token in seq:
            # copy the entity
            if token>=len(text_vocab):
                ent_text = batch['raw_ent_text'][b][token-len(text_vocab)]
                ent_text = filter(lambda x:x!='<PAD>', ent_text)
                txt.extend(ent_text)
            else:
                if int(token) not in [text_vocab(x) for x in ['<PAD>', '<BOS>', '<EOS>']]:
                    txt.append(text_vocab(int(token)))
            if int(token) == text_vocab('<EOS>'):
                break
        ret.append(' '.join([str(x) for x in txt]))
    return ret 

def _write_txt(batch, seqs, w_file, args):
    # converting the prediction to real text.
    ret = []
    for b, seq in enumerate(seqs):
        txt = []
        for token in seq:
            # copy the entity
            if token>=len(args.text_vocab):
                ent_text = batch['raw_ent_text'][b][token-len(args.text_vocab)]
                ent_text = filter(lambda x:x!='<PAD>', ent_text)
                txt.extend(ent_text)
            else:
                if int(token) not in [args.text_vocab(x) for x in ['<PAD>', '<BOS>', '<EOS>']]:
                    txt.append(args.text_vocab(int(token)))
            if int(token) == args.text_vocab('<EOS>'):
                break
        w_file.write(' '.join([str(x) for x in txt])+'\n')
        ret.append([' '.join([str(x) for x in txt])])
    return ret 

def replace_ent(x, ent, V):
    # replace the entity
    mask = x>=V
    if mask.sum()==0:
        return x
    nz = mask.nonzero()
    fill_ent = ent[nz, x[mask]-V]
    x = x.masked_scatter(mask, fill_ent)
    return x

def len2mask(lens, device):
    max_len = max(lens)
    mask = torch.arange(max_len, device=device).unsqueeze(0).expand(len(lens), max_len)
    mask = mask >= torch.LongTensor(lens).to(mask).unsqueeze(1)
    return mask

def pad(var_len_list, out_type='list', flatten=False):
    if flatten:
        lens = [len(x) for x in var_len_list]
        var_len_list = sum(var_len_list, [])
    max_len = max([len(x) for x in var_len_list])
    if out_type=='list':
        if flatten:
            return [x+['<PAD>']*(max_len-len(x)) for x in var_len_list], lens
        else:
            return [x+['<PAD>']*(max_len-len(x)) for x in var_len_list]
    if out_type=='tensor':
        if flatten:
            return torch.stack([torch.cat([x, \
            torch.zeros([max_len-len(x)]+list(x.shape[1:])).type_as(x)], 0) for x in var_len_list], 0), lens
        else:
            return torch.stack([torch.cat([x, \
            torch.zeros([max_len-len(x)]+list(x.shape[1:])).type_as(x)], 0) for x in var_len_list], 0)

class Vocab(object):
    def __init__(self, max_vocab=2**31, min_freq=-1, sp=['<PAD>', '<BOS>', '<EOS>', '<UNK>']):
        self.i2s = []
        self.s2i = {}
        self.wf = {}
        self.max_vocab, self.min_freq, self.sp = max_vocab, min_freq, sp

    def __len__(self):
        return len(self.i2s)

    def __str__(self):
        return 'Total ' + str(len(self.i2s)) + str(self.i2s[:10])

    def update(self, token):
        if isinstance(token, list):
            for t in token:
                self.update(t)
        else:
            self.wf[token] = self.wf.get(token, 0) + 1

    def build(self):
        self.i2s.extend(self.sp)
        sort_kv = sorted(self.wf.items(), key=lambda x:x[1], reverse=True)
        for k,v in sort_kv:
            if len(self.i2s)<self.max_vocab and v>=self.min_freq and k not in self.sp:
                self.i2s.append(k)
        self.s2i.update(list(zip(self.i2s, range(len(self.i2s)))))

    def __call__(self, x):
        if isinstance(x, int):
            return self.i2s[x]
        else:
            return self.s2i.get(x, self.s2i['<UNK>'])
    
    def save(self, fname):
        pass

    def load(self, fname):
        pass

def at_least(x):
    # handling the illegal data
    if len(x) == 0:
        return ['<UNK>']
    else:
        return x

def get_graph(ent_len, rel_len, adj_edges):
    graph = dgl.DGLGraph()

    graph.add_nodes(ent_len,
                    {'type': torch.ones(ent_len) * NODE_TYPE['entity']})
    graph.add_nodes(1, {'type': torch.ones(1) * NODE_TYPE['root']})
    graph.add_nodes(rel_len * 2,
                    {'type': torch.ones(rel_len * 2) * NODE_TYPE['relation']})
    graph.add_edges(ent_len, torch.arange(ent_len))
    graph.add_edges(torch.arange(ent_len), ent_len)
    graph.add_edges(torch.arange(ent_len + 1 + rel_len * 2),
                    torch.arange(ent_len + 1 + rel_len * 2))

    if len(adj_edges) > 0:
        graph.add_edges(*list(map(list, zip(*adj_edges))))
    return graph

class Example(object):
    def __init__(self):
        self.raw_title = None
        self.raw_ent_text = None
        self.raw_ent_type = None
        self.raw_triples = None
        self.raw_text = None
        self.raw_t2g_text = None
        self.t2g_combinations = None
        self.graph = None
        self.id = None
        self.mode = None

    def __str__(self):
        return '\n'.join(
            [str(k) + ':\t' + str(v) for k, v in self.__dict__.items()])

    def __len__(self):
        return len(self.raw_text)

    def build_graph(self):
        ent_len = len(self.raw_ent_text)
        raw_rel = [x[1] for x in self.raw_triples]
        rel_set = sorted(list(set(raw_rel)))
        rel_len = len(raw_rel) # treat the repeated relation as different nodes, refer to the author's code

        adj_edges = []
        for i, r in enumerate(self.raw_triples):
            assert len(r)==3, str(r)
            st, rt, ed = r
            st_ent, ed_ent = self.raw_ent_text.index(st), self.raw_ent_text.index(ed)
            # according to the edge_softmax operator, we need to reverse the graph
            adj_edges.append([ent_len+1+2*i, st_ent])
            adj_edges.append([ed_ent, ent_len+1+2*i])
            adj_edges.append([ent_len+1+2*i+1, ed_ent])
            adj_edges.append([st_ent, ent_len+1+2*i+1])

        graph = get_graph(ent_len, rel_len, adj_edges)
        return graph

    def get_tensor(self, ent_vocab, rel_vocab, text_vocab, ent_text_vocab, title_vocab):
        if hasattr(self, '_cached_tensor'):
            return self._cached_tensor
        else:
            title_data = ['<BOS>'] + self.raw_title + ['<EOS>']
            title = [title_vocab(x) for x in title_data]
            ent_text = [[ent_text_vocab(y) for y in x] for x in self.raw_ent_text]
            ent_type = [text_vocab(x) for x in self.raw_ent_type] # for inference
            rel_data = ['--root--'] + sum([[x[1],x[1]+'_INV'] for x in self.raw_triples], [])
            rel = [rel_vocab(x) for x in rel_data]

            text_data = ['<BOS>'] + self.raw_text + ['<EOS>']
            text = [text_vocab(x) for x in text_data]
            tgt_text = []
            # the input text and decoding target are different since the consideration of the copy mechanism.
            for i, str1 in enumerate(text_data):
                if str1[0]=='<' and str1[-1]=='>' and '_' in str1:
                    a, b = str1[1:-1].split('_')
                    text[i] = text_vocab('<'+a+'>')
                    tgt_text.append(len(text_vocab)+int(b))
                else:
                    tgt_text.append(text[i])
            self._cached_tensor = {'title': torch.LongTensor(title), 'ent_text': [torch.LongTensor(x) for x in ent_text], \
                                'ent_type': torch.LongTensor(ent_type), 'rel': torch.LongTensor(rel), \
                                'text': torch.LongTensor(text[:-1]), 'tgt_text': torch.LongTensor(tgt_text[1:]), 'graph': self.graph, 'raw_ent_text': self.raw_ent_text}
            return self._cached_tensor

    def update_vocab(self, ent_vocab, rel_vocab, text_vocab, ent_text_vocab, title_vocab):
        ent_vocab.update(self.raw_ent_type)
        ent_text_vocab.update(self.raw_ent_text)
        title_vocab.update(self.raw_title)
        rel_vocab.update(['--root--']+[x[1] for x in self.raw_triples]+[x[1]+'_INV' for x in self.raw_triples])
        text_vocab.update(self.raw_ent_type)
        text_vocab.update(self.raw_text)


class AgendaExample(Example):
    def __init__(self, title, ent_text, ent_type, rel, text):
        # one object corresponds to a data sample
        super().__init__()
        self.raw_title = title.split()
        self.raw_ent_text = [at_least(x.split()) for x in ent_text]
        assert min([len(x) for x in self.raw_ent_text])>0, str(self.raw_ent_text)
        self.raw_ent_type = ent_type.split() # <method> .. <>
        self.raw_triples = []
        for r in rel:
            rel_list = r.split()
            for i in range(len(rel_list)):
                if i>0 and i<len(rel_list)-1 and rel_list[i-1]=='--' and rel_list[i]!=rel_list[i].lower() and rel_list[i+1]=='--':
                    self.raw_triples.append([rel_list[:i-1], rel_list[i-1]+rel_list[i]+rel_list[i+1], rel_list[i+2:]])
                    break
        self.raw_text = text.split()
        self.graph = self.build_graph()

    @staticmethod
    def from_json(json_data):
        return AgendaExample(json_data['title'], json_data['entities'], json_data['types'],
                json_data['relations'], json_data['abstract'])

class WebNLGExample(Example):
    def __init__(self, ner2ent, triples, text):
        super().__init__()

        self.raw_title = []
        sorted_ner2ent = sorted(ner2ent.items())
        ners, ents = zip(*sorted_ner2ent)
        self.raw_ent_text = [x.split() for x in ents]
        assert min([len(x) for x in self.raw_ent_text]) > 0, str(
            self.raw_ent_text)
        self.raw_ent_type = ['<' + x.split('_')[0] + '>' for x in ners]
        self.raw_triples = [[st.split(), r, ed.split()] for st, r, ed in
                            triples]
        self.raw_text = []
        self.raw_t2g_text = []
        for x in text.split():
            if x in ner2ent:
                ner = x.split('_')[0]
                ix = ners.index(x)
                tok = '<{}_{}>'.format(ner, ix)
                self.raw_text.append(tok)
                self.raw_t2g_text.extend(self.raw_ent_text[ix])
            else:
                self.raw_text.append(x)
                self.raw_t2g_text.append(x)
        self.graph = self.build_graph()

    @staticmethod
    def from_json(json_data):
        return WebNLGExample(json_data['ner2ent'],
                             json_data['triples'],
                             json_data['target'])

class BucketSampler(torch.utils.data.Sampler):
    def __init__(self, data_source, batch_size=32, bucket=3):
        self.data_source = data_source
        self.bucket = bucket
        self.batch_size = batch_size

    def __iter__(self):
        # the magic number comes from the author's code
        perm = torch.randperm(len(self.data_source))
        lens = torch.Tensor([len(x) for x in self.data_source])
        lens = lens[perm]
        t1 = []
        t2 = []
        t3 = []
        for i, l in enumerate(lens):
            if (l<100):
                t1.append(perm[i])
            elif (l>100 and l<220):
                t2.append(perm[i])
            else:
                t3.append(perm[i])
        datas = [t1,t2,t3]
        random.shuffle(datas)
        idxs = sum(datas, [])
        batch = []
        for idx in idxs:
            batch.append(idx)
            mlen = max([0]+[lens[x] for x in batch])
            if (mlen<100 and len(batch) == 32) or (mlen>100 and mlen<220 and len(batch) >= 24) or (mlen>220 and len(batch)>=8) or len(batch)==32:
                yield batch
                batch = []
        if len(batch) > 0:
            yield batch

    def __len__(self):
        return (len(self.data_source)+self.batch_size-1)//self.batch_size
        

class GWdataset(torch.utils.data.Dataset):
    def __init__(self, exs, ent_vocab=None, rel_vocab=None, text_vocab=None, ent_text_vocab=None, title_vocab=None, device=None, vocab_pack=None):
        super(GWdataset, self).__init__()
        self.exs = exs
        device = torch.device(device)
        if vocab_pack is not None:
            ent_vocab = vocab_pack['ent_vocab']
            rel_vocab = vocab_pack['rel_vocab']
            text_vocab = vocab_pack['text_vocab']
            ent_text_vocab = vocab_pack['ent_text_vocab']
            title_vocab = vocab_pack['title_vocab']
        self.ent_vocab, self.rel_vocab, self.text_vocab, self.ent_text_vocab, self.title_vocab, self.device = \
        ent_vocab, rel_vocab, text_vocab, ent_text_vocab, title_vocab, device

    def __iter__(self):
        return iter(self.exs)

    def __getitem__(self, index):
        return self.exs[index]

    def __len__(self):
        return len(self.exs)

    def batch_fn(self, batch_ex):
        batch_title, batch_ent_text, batch_ent_type, batch_rel, batch_text, batch_tgt_text, batch_graph = \
        [], [], [], [], [], [], []
        batch_raw_ent_text = []
        for ex in batch_ex:
            ex_data = ex.get_tensor(self.ent_vocab, self.rel_vocab, self.text_vocab, self.ent_text_vocab, self.title_vocab)
            batch_title.append(ex_data['title'])
            batch_ent_text.append(ex_data['ent_text'])
            batch_ent_type.append(ex_data['ent_type'])
            batch_rel.append(ex_data['rel'])
            batch_text.append(ex_data['text'])
            batch_tgt_text.append(ex_data['tgt_text'])
            batch_graph.append(ex_data['graph'])
            batch_raw_ent_text.append(ex_data['raw_ent_text'])
        batch_title = pad(batch_title, out_type='tensor')
        batch_ent_text, ent_len = pad(batch_ent_text, out_type='tensor', flatten=True)
        batch_ent_type = pad(batch_ent_type, out_type='tensor')
        batch_rel = pad(batch_rel, out_type='tensor')
        batch_text = pad(batch_text, out_type='tensor')
        batch_tgt_text = pad(batch_tgt_text, out_type='tensor')
        batch_graph = dgl.batch(batch_graph)
        batch_graph.to(self.device)
        return {'title': batch_title.to(self.device), 'ent_text': batch_ent_text.to(self.device), 'ent_len': ent_len, \
            'ent_type': batch_ent_type.to(self.device), 'rel': batch_rel.to(self.device), 'text': batch_text.to(self.device), \
            'tgt_text': batch_tgt_text.to(self.device), 'graph': batch_graph, 'raw_ent_text': batch_raw_ent_text}

def get_one_dataset(fname, dataset_type, device, vocab_pack):
    if dataset_type == 'webnlg':
        example_class = WebNLGExample
    if dataset_type == 'agenda':
        example_class = AgendaExample
    exs = []
    json_datas = json.loads(open(fname).read())
    for json_data in json_datas:
        # construct one data example
        ex = example_class.from_json(json_data)
        exs.append(ex)
    return GWdataset(exs, vocab_pack=vocab_pack, device=device)

def get_vocab(fnames, no_save=True, min_freq=5, save='vocab.pickle'):
    ent_vocab = Vocab(sp=['<PAD>', '<UNK>']) 
    title_vocab = Vocab(min_freq=min_freq) 
    rel_vocab = Vocab(sp=['<PAD>', '<UNK>'])
    text_vocab = Vocab(min_freq=min_freq)
    ent_text_vocab = Vocab(sp=['<PAD>', '<UNK>'])
    if 'webnlg' in fnames[0]:
        example_class = WebNLGExample
    else:
        example_class = AgendaExample
    for fname in fnames:
        json_datas = json.loads(open(fname).read())
        for json_data in json_datas:
            # construct one data example
            ex = example_class.from_json(json_data)
            ex.update_vocab(ent_vocab, rel_vocab, text_vocab, ent_text_vocab, title_vocab)
    ent_vocab.build()
    rel_vocab.build()
    text_vocab.build()
    ent_text_vocab.build()
    title_vocab.build()
    vocab_pack = {'ent_vocab': ent_vocab, 'rel_vocab': rel_vocab, 'text_vocab': text_vocab, 'ent_text_vocab': ent_text_vocab, 'title_vocab': title_vocab}
    if not no_save:
        with open(save, 'wb') as f:
            pickle.dump(vocab_pack, f)
    return vocab_pack

def get_datasets(fnames, min_freq=-1, sep=';', joint_vocab=True, device=None, save='tmp.pickle'):
    # min_freq : not support now since it's very sensitive to the final results, but you can set it via passing min_freq to the Vocab class.
    # sep : not support now
    # joint_vocab : not support now
    ent_vocab = Vocab(sp=['<PAD>', '<UNK>']) 
    title_vocab = Vocab(min_freq=5) 
    rel_vocab = Vocab(sp=['<PAD>', '<UNK>'])
    text_vocab = Vocab(min_freq=5)
    ent_text_vocab = Vocab(sp=['<PAD>', '<UNK>'])
    datasets = []
    if 'webnlg' in fnames[0]:
        example_class = WebNLGExample
    else:
        example_class = AgendaExample
    for fname in fnames:
        exs = []
        json_datas = json.loads(open(fname).read())
        for json_data in json_datas:
            # construct one data example
            ex = example_class.from_json(json_data)
            if fname == fnames[0]: # only training set
                ex.update_vocab(ent_vocab, rel_vocab, text_vocab, ent_text_vocab, title_vocab)
            exs.append(ex)
        datasets.append(exs)
    ent_vocab.build()
    rel_vocab.build()
    text_vocab.build()
    ent_text_vocab.build()
    title_vocab.build()
    datasets = [GWdataset(exs, ent_vocab, rel_vocab, text_vocab, ent_text_vocab, title_vocab, device) for exs in datasets]
    with open(save, 'wb') as f:
        pickle.dump(datasets, f)
    return datasets

if __name__ == '__main__' :
    ds = get_datasets(['data/unprocessed.val.json', 'data/unprocessed.val.json', 'data/unprocessed.test.json'])
    print(ds[0].exs[0])
    print(ds[0].exs[0].get_tensor(ds[0].ent_vocab, ds[0].rel_vocab, ds[0].text_vocab, ds[0].ent_text_vocab, ds[0].title_vocab))

