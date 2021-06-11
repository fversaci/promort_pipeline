from cassandra.auth import PlainTextAuthProvider
from cassandra_dataset import CassandraDataset
import time
from tqdm import trange, tqdm
import numpy as np
import pyecvl.ecvl as ecvl
import pyeddl.eddl as eddl
from pyeddl.tensor import Tensor
import random

from MMPI import miniMPI

def set_snet_params(net, new_weights, div, format='numpy'):
    sn = net.snets[0]
    for l_index, w_l in enumerate(new_weights):
        if w_l: # It is an actual layer
            if format == 'numpy':
                bias = Tensor(w_l[0])
                weights = Tensor(w_l[1])
            elif format == 'eddlT':
                bias = w_l[0] * div
                weights = w_l[1] * div
            
            Tensor.copy(bias, sn.layers[l_index].params[0])
            Tensor.copy(weights, sn.layers[l_index].params[1])


def set_net_weights(net, new_weights):
    #new_weights_tens = [[Tensor(new_weights[i][j]) for j, _ in enumerate(l)] for i, l in enumerate(new_weights)]
    #eddl.set_parameters(net, new_weights_tens)
    l_l = net.layers
    for i, l in enumerate(l_l):
        w_l = new_weights[i]
        if w_l:
            bias = Tensor(w_l[0])
            weights = Tensor(w_l[1])
            l.update_weights(bias, weights)
            eddl.distributeParams(l)


def zero_weights_like(w):
    zero_weights = [[np.zeros_like(w[i][j]) for j, _ in enumerate(l)] for i, l in enumerate(w)]
    return zero_weights


def acc_weights(acc, w):
    if acc:
        acc = [[acc[i][j] + (w[i][j]) for j, _ in enumerate(l)] for i, l in enumerate(acc)]
    else:
        acc = [[w[i][j].getdata() for j, _ in enumerate(l)] for i, l in enumerate(w)]
    return acc    


def sync_data(MP, data):
    # Weight lists, ltr losses, ltr metric 
    #print("Sync Function")
    #t0 = time.time()
    mpi_size = MP.mpi_size

    w, l, m = data
    
    ltr = l.shape[0]

    # Averaging Local weights, loss and metric
    local_weights_avg = [[w[i][j] / ltr for j, _ in enumerate(l)] for i, l in enumerate(w)]
    local_loss_avg = float(np.mean(l))
    local_metric_avg = float(np.mean(m))

    #t1 = time.time()
    #print("Reduce function: Average of structures time: %.3f" % (t1-t0))
    #t0 = t1

    # Global Average of weights, metric and loss 
    glob_weights = [[np.empty_like(local_weights_avg[i][j]) for j, _ in enumerate(l)] for i, l in enumerate(local_weights_avg)]
    MP.LoLAverage(local_weights_avg, glob_weights) 
    
    #div = 1/MP.mpi_size
    #glob_weights = [[(glob_weights[i][j] * div) for j, _ in enumerate(l)] for i, l in enumerate(glob_weights)]

    glob_losses = MP.Allreduce(local_loss_avg, 'SUM') / mpi_size

    glob_metrics = MP.Allreduce(local_metric_avg, 'SUM') / mpi_size

    #t1 = time.time()
    #print("Reduce function: mpi time Communication: %.3f" % (t1-t0))

    return (glob_weights, glob_losses, glob_metrics)


def train(MP, ltr, el, init_weights_fn, epochs, sync_iterations, lr, gpus, dropout, l2_reg, seed):
    
    rank = MP.mpi_rank
    div = 1.0

    print('Starting train function')
    t0 = time.time()

    # Get Environment
    el.start(seed)
    cd = el.cd
    net = el.net
    out = net.layers[-1]

    t1 = time.time()
    print("Time to load the Environment  %.3f" % (t1-t0))
    t0 = t1

    # Loading model weights if any
    if init_weights_fn:
        eddl.load(net, init_weights_fn)

    ###################
    ## Training step ##
    ###################
    
    print("Defining metric...", flush=True)
    
    metric_fn = eddl.getMetric("categorical_accuracy")
    loss_fn = eddl.getLoss("soft_cross_entropy")

    print("Starting training", flush=True)

    if rank == 0: # Only task 0 takes account of whole stats
        loss_l = []
        metric_l = []
        val_loss_l = []
        val_acc_l = []
        
        patience_cnt = 0
        val_acc_max = 0.0

    local_split_indexes = [rank*ltr + i for i in range(ltr)]
   
    metric_fn = eddl.getMetric("categorical_accuracy")
    loss_fn = eddl.getLoss("soft_cross_entropy")


    # Get initial weights from rank 0
    glob_weights = eddl.get_parameters(net) #
    MP.LoLBcast(glob_weights, root=0)
 
    
    ### Main loop across epochs
    t0 = time.time()
    for e in range(epochs):
        ### Training 
        ### Recreate splits to shuffle among workers but with the same seed to get same splits
        eddl.reset_loss(net)
        seed = random.getrandbits(32)
        el.split_setup(seed)
    
        # Shuffle local splits and get the number of batches
        for sp in local_split_indexes: cd.rewind_splits(sp, shuffle=True)
        local_num_batches = [cd.num_batches[i] for i in local_split_indexes]
        num_batches = min(cd.num_batches) # FIXME: Using the minimum among all batches not the local ones
        macro_batches = num_batches // sync_iterations
        
        if rank == 0:
            epoch_metric_l = []
            epoch_loss_l = []
        
        pbar = tqdm(range(macro_batches))
        
        for mb_index, mb in enumerate(pbar):
            # Init local weights to a zero structure equal in size and shape to the global one
            t0 = time.time()
            local_weights_acc = None 
            #local_weights_acc = zero_weights_like(glob_weights) 
            per_ltr_loss = np.zeros(ltr)
            per_ltr_metric = np.zeros(ltr)
            
            t1 = time.time()
            print (f"Time to start macrobatch datastructs: {t1-t0}") 
            t0 = time.time()
            
            
            for lt in range(ltr):
                # set global weights before start training
                #set_net_weights(net, glob_weights)
                set_snet_params(net, glob_weights, div)
                div = 1 / MP.mpi_size
                
                t1 = time.time()
                print (f"Time to set weights to ltr {lt}: {t1-t0}") 
                t0 = time.time()
                
                split_index = rank * ltr + lt # Local split. If ltr == 1 --> split_index = rank
                
                # Looping across batches before local 
                # network weights are averaged among workers nets

                for s_it in range(sync_iterations):
                    x, y = cd.load_batch(split_index)
                    x.div_(255.0)
                    tx, ty = [x], [y]
                    
                    #print (f'Train batch rank: {rank}, ep: {e}, macro_batch: {mb}, local training rank: {lt}, inidipendent iteration: {s_it}') 
                    eddl.train_batch(net, tx, ty)
                    
                    net_out = eddl.getOutput(net.layers[-1]).getdata() 
                    loss = eddl.get_losses(net)[0]
                    metric = eddl.get_metrics(net)[0]
                
                    per_ltr_loss[lt] += loss
                    per_ltr_metric[lt] += metric
                

                t1 = time.time()
                print (f"Time to Compute microbatches of ltr {lt}: {t1-t0}") 
                t0 = time.time()
                
                ## End Iterations on the batches of a single local training rank
                ## Accumulate weights of local training rank.                
                per_ltr_loss[lt] /= sync_iterations
                per_ltr_metric[lt] /= sync_iterations
                net_params = eddl.get_parameters(net)
                local_weights_acc = acc_weights(local_weights_acc, net_params)
                t1 = time.time()
                print (f"Time to accumulate weights of ltr {lt}: {t1-t0}") 
                t0 = time.time()
                
            ## End of the training of all ltr
            # Global Sync: Local network weights are averaged and then averaged among rank nodes. Same for loss and metric
            data = (local_weights_acc, per_ltr_loss, per_ltr_metric)
            glob_weights, glob_loss, glob_metric = sync_data(MP, data)
            
            t1 = time.time()
            print (f"Time to synch data: {t1-t0}") 
            t0 = time.time()
            
            if rank == 0:
                msg = "Epoch {:d}/{:d} (macro batch {:d}/{:d}) - loss: {:.3f}, acc: {:.3f}".format(e + 1, epochs, mb + 1, macro_batches, glob_loss, glob_metric)
                pbar.set_postfix_str(msg)
                epoch_loss_l.append(glob_loss)
                epoch_metric_l.append(glob_metric)
            
        ## End of macro batches
        pbar.close()
        
        # Compute Epoch loss and metric and store history
        if rank == 0:
            loss_l.append(np.mean(epoch_loss_l))
            metric_l.append(np.mean(epoch_metric_l))

            if out_dir:
                history = {'loss': loss_l, 'acc': acc_l, 'val_loss': val_loss_l, 'val_acc': val_acc_l}
                pickle.dump(history, open(os.path.join(res_dir, 'history.pickle'), 'wb'))
        
    ## End of Epochs
    if rank == 0:
        return loss_l, metric_l, val_loss_l, val_acc_l
    else:
        return None, None, None, None