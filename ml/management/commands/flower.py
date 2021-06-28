
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from copy import deepcopy
from time import time

from ml.management.commands.utilities import extract_grad, sp, nb_params, models_dist
from ml.management.commands.utilities import model_norm, round_loss, tens_count, node_local_loss, one_hot_vids
#from ml.management.commands.utilities import get_node_vids, get_all_vids


def get_classifier(nb_vids, gpu=False):
    ''' returns one layer model for one-hot entries '''
    model = nn.Sequential(nn.Linear(nb_vids, 1, bias=False))
    if gpu:
        return model.cuda()
    return model

# nodes organisation
class Flower():
    ''' Training structure including local models and general one 
        Allowing to add and remove nodes at will
        .pop
        .add_nodes
        .rem_nodes
        .train
        .display
        .check
    '''

    def __init__(self, nb_vids, dic, gpu=False, **kwargs):
        ''' opt : optimizer
            test : test data couple (imgs,labels)
            w0 : regularisation strength
        '''
        #self.d_test = test
        self.w0 = 0.2
        self.w = 0.05
        self.gpu = gpu

        self.opt = optim.Adam
        self.lr_node = 0.2
        self.lr_gen = 0.2
        self.gen_freq = 1  # generalisation frequency (>=1)

        self.get_classifier = get_classifier
        self.general_model = self.get_classifier(nb_vids, gpu)
        self.init_model = deepcopy(self.general_model)
        self.last_grad = None
        self.opt_gen = self.opt(self.general_model.parameters(), lr=self.lr_gen)
        self.pow_gen = (1,1)  # choice of norms for Licchavi loss 
        self.pow_reg = (2,1)  # (internal power, external power)
        self.data = []  # includes labels
        # self.labels = [] 
        self.typ = []
        self.models = []
        self.weights = []
        self.age = []
        self.user_ids = []
        self.opt_nodes = []
        self.nb_nodes = 0
        self.dic = dic
        self.history = ([], [], [], [], [], [], [], []) 
        # self.h_legend = ("fit", "gen", "reg", "acc", "l2_dist", "l2_norm", "grad_sp", "grad_norm")
        # self.localtest = ([], []) # (which to pick for each node, list of (data,labels) pairs)
        self.nb_params = nb_vids
        self.size = nb_params(self.general_model) / 10_000

    # ------------ population methods --------------------

    def set_allnodes(self, data_distrib, user_ids, verb=1):
        ''' put data in Flower and create a model for each node '''
        self.data = data_distrib
        self.user_ids = user_ids
        nb = len(self.data)

        self.typ += ["unkwown"] * nb

        self.models += [self.get_classifier(self.nb_params, self.gpu) for i in range(nb)]
        self.weights += [self.w] * nb
        self.age = [0] * nb
        # for i in range(nb):
        #     self.localtest[0].append(-1)
        self.nb_nodes = nb
        self.opt_nodes = [self.opt(self.models[n].parameters(), lr=self.lr_node) 
                            for n in range(nb) ]
        if verb:
            print("Total number of nodes : {}".format(self.nb_nodes))


    # def set_localtest(self, datafull, size, nodes, fav_lab=(0,0), typ="honest"):
    #     ''' create a local data for some nodes
    #         datafull : source data
    #         size : size of test sample
    #         fav_labs : (label, strength)
    #         nodes : list of nodes which use this data           
    #     '''
    #     id = self.dic[typ]
    #     dish = (id != -1) # boolean for dishonesty
    #     dt, lb = distribute_data_rd(datafull, [size], fav_lab,
    #                                 dish, dish_lab=id, gpu=self.gpu)
    #     dtloc = (dt[0], lb[0])
    #     self.localtest[1].append(dtloc)
    #     id = len(self.localtest[1]) - 1
    #     for n in nodes:
    #         self.localtest[0][n] = id

    def add_nodes(self, datafull, pop, typ, fav_lab=(0,0), verb=1, **kwargs):
        ''' add nodes to the Flower 
            datafull : data to put on node (sampled from it)
            pop : (nb of nodes, size of nodes)
            typ : type of nodes (str keywords)
            fav_lab : (favorite label, strength)
            w : int, weight of new nodes
        '''
        w = kwargs["w"] # taking global variable if -w not provided
        nb, size = pop
        id = self.dic[typ]
        dish = (id != -1) # boolean for dishonesty
        dt, lb = distribute_data_rd(datafull, [size] * nb, fav_lab,
                                    dish, dish_lab=id, gpu=self.gpu)
        self.data += dt
        self.labels += lb
        self.typ += [typ] * nb

        self.models += [self.get_classifier(self.gpu) for i in range(nb)]
        self.weights += [w] * nb
        self.age += [0] * nb
        for i in range(nb):
            self.localtest[0].append(-1)
        self.nb_nodes += nb
        self.opt_nodes += [self.opt(self.models[n].parameters(), lr=self.lr_node) 
            for n in range(self.nb_nodes - nb, self.nb_nodes) 
            ]
        if verb:
            print("Added {} {} nodes of {} data points".format(nb, typ, size))
            print("Total number of nodes : {}".format(self.nb_nodes))

    def rem_nodes(self, first, last, verb=1):
        ''' remove nodes of indexes -first (included) to -last (excluded) '''
        nb = last - first
        if last > self.nb_nodes:
            print("-last is out of range, remove canceled")
        else:
            del self.data[first : last]
            del self.labels[first : last] 
            del self.typ[first : last]
            del self.models[first : last]
            del self.weights[first : last]
            del self.age[first : last]
            del self.opt_nodes[first : last]
            del self.localtest[0][first : last]
            self.nb_nodes -= nb
            if verb: print("Removed {} nodes".format(nb))
        

    def output_scores(self):
        ''' Returns video scores both local and global '''
        local_scores = []
        list_ids_batchs = []
        with torch.no_grad():
            for n, node in enumerate(self.data):
                input = one_hot_vids(self.dic, node[3])
                output = self.models[n](input)
                local_scores.append(output)
                list_ids_batchs.append(node[3])
            for p in self.general_model.parameters():  # only one iteration   
                glob_scores = p[0]
            vids_batch = list(self.dic.keys())
        return (vids_batch, glob_scores), (list_ids_batchs, local_scores)

    # ---------- methods for training ------------

    def _set_lr(self):
        '''set learning rates of optimizers according to Flower setting'''
        for n in range(self.nb_nodes):  # updating lr in optimizers
            self.opt_nodes[n].param_groups[0]['lr'] = self.lr_node
        self.opt_gen.param_groups[0]['lr'] = self.lr_gen

    def _zero_opt(self):
        '''reset gradients of all models'''
        for n in range(self.nb_nodes):
            self.opt_nodes[n].zero_grad()      
        self.opt_gen.zero_grad()

    def _update_hist(self, epoch, test_freq, fit, gen, reg, verb=1):
        ''' update history '''
        # if epoch  % test_freq == 0:   # printing accuracy on test data
        #     acc = self.score_glob(self.d_test)
        #     if verb: print("TEST ACCURACY : ", acc)
        #     for i in range(test_freq):
        #         self.history[3].append(acc) 
        self.history[0].append(round_loss(fit))
        self.history[1].append(round_loss(gen))
        self.history[2].append(round_loss(reg))

        dist = models_dist(self.init_model, self.general_model, pow=(2,0.5)) 
        norm = model_norm(self.general_model, pow=(2,0.5))
        self.history[4].append(round_loss(dist, 1))
        self.history[5].append(round_loss(norm, 1))
        grad_gen = extract_grad(self.general_model)
        if epoch > 1: # no last model for first epoch
            scal_grad = sp(self.last_grad, grad_gen)
            self.history[6].append(scal_grad)
        else:
            self.history[6].append(0) # default value for first epoch
        self.last_grad = deepcopy(extract_grad(self.general_model)) 
        grad_norm = sp(grad_gen, grad_gen)  # use sqrt ?
        self.history[7].append(grad_norm)

    def _old(self, years):
        ''' increment age (after training) '''
        for i in range(self.nb_nodes):
            self.age[i] += years

    def _counters(self, c_gen, c_fit):
        '''update internal training counters'''
        fit_step = (c_fit >= c_gen) 
        if fit_step:
            c_gen += self.gen_freq
        else:
            c_fit += 1 
        return fit_step, c_gen, c_fit

    def _do_step(self, fit_step):
        '''step for appropriate optimizer(s)'''
        if fit_step:       # updating local or global alternatively
            for n in range(self.nb_nodes): 
                self.opt_nodes[n].step()      
        else:
            self.opt_gen.step()  

    def _print_losses(self, tot, fit, gen, reg):
        '''print losses'''
        print("total loss : ", tot) 
        print("fitting : ", round_loss(fit),
                ', generalisation : ', round_loss(gen),
                ', regularisation : ', round_loss(reg))

    # ====================  TRAINING ================== 

    def train(self, nb_epochs=None, test_freq=1, verb=1):   
        '''training loop'''
        nb_epochs = 2 if nb_epochs is None else nb_epochs
        time_train = time()
        self._set_lr()

        # initialisation to avoid undefined variables at epoch 1
        loss, fit_loss, gen_loss, reg_loss = 0, 0, 0, 0
        c_fit, c_gen = 0, 0

        fit_scale = 20 / self.nb_nodes
        gen_scale = 1 / self.nb_nodes / self.size
        reg_scale = self.w0 / self.size

        reg_loss = reg_scale * model_norm(self.general_model, self.pow_reg)  

        # training loop 
        nb_steps = self.gen_freq + 1
        for epoch in range(1, nb_epochs + 1):
            if verb: print("\nepoch {}/{}".format(epoch, nb_epochs))
            time_ep = time()

            for step in range(1, nb_steps + 1):
                fit_step, c_gen, c_fit = self._counters(c_gen, c_fit)
                if verb >= 2: 
                    txt = "(fit)" if fit_step else "(gen)" 
                    print("step :", step, '/', nb_steps, txt)
                self._zero_opt() # resetting gradients


                #----------------    Licchavi loss  -------------------------
                 # only first 2 terms of loss updated
                if fit_step:
                    fit_loss, gen_loss = 0, 0
                    for n in range(self.nb_nodes):   # for each node
                        s = torch.ones(1)  # user notation style, constant for now
                        fit_loss += node_local_loss(self.models[n], s,  self.data[n][0],
                                                                        self.data[n][1], 
                                                                        self.data[n][2])
                        g = models_dist(self.models[n], self.general_model, self.pow_gen)
                        gen_loss +=  self.weights[n] * g  # generalisation term
                    fit_loss *= fit_scale
                    gen_loss *= gen_scale
                    loss = fit_loss + gen_loss 
                          
                # only last 2 terms of loss updated 
                else:        
                    gen_loss, reg_loss = 0, 0
                    for n in range(self.nb_nodes):   # for each node
                        g = models_dist(self.models[n], 
                                        self.general_model, self.pow_gen)
                        gen_loss += self.weights[n] * g  # generalisation term    
                    reg_loss = model_norm(self.general_model, self.pow_reg) 
                    gen_loss *= gen_scale
                    reg_loss *= reg_scale
                    loss = gen_loss + reg_loss

                total_out = round_loss(fit_loss + gen_loss + reg_loss)
                if verb >= 2:
                    self._print_losses(total_out, fit_loss, gen_loss, reg_loss)
                # Gradient descent 
                loss.backward() 
                self._do_step(fit_step)   
 
            if verb: print("epoch time :", round(time() - time_ep, 2)) 
            self._update_hist(epoch, test_freq, fit_loss, gen_loss, reg_loss, verb)
            self._old(1)  # aging all nodes
             
        # ----------------- end of training -------------------------------  
        #for i in range(nb_epochs % test_freq): # to maintain same history length
         #   self.history[3].append(acc)
        print("training time :", round(time() - time_train, 2)) 
        return self.history


    # ------------ to check for problems --------------------------
    def check(self):
        ''' perform some tests on internal parameters adequation '''
        # population check
        b1 =  (self.nb_nodes == len(self.data) == len(self.labels) 
            == len(self.typ) == len(self.models) == len(self.opt_nodes) 
            == len(self.weights) == len(self.age) == len(self.localtest[0]))
        # history check
        b2 = True
        for l in self.history:
            b2 = b2 and (len(l) == len(self.history[0]) >= max(self.age))
        # local test data check
        b3 = (max(self.localtest[0]) + 1 <= len(self.localtest[1]) )
        if (b1 and b2 and b3):
            print("No Problem")
        else:
            print("OULALA non ça va pas là")


def get_flower(nb_vids, dic, gpu=False, **kwargs):
    '''get a Flower using the appropriate test data (gpu or not)'''
   # if gpu:
    #    return Flower(test_gpu, gpu=gpu, **kwargs)
    #else:
     #   return Flower(test, gpu=gpu, **kwargs)
    return Flower(nb_vids, dic, gpu=gpu, **kwargs)

