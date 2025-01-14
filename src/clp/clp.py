import warnings
from typing import Optional, Sequence
import numpy as np
import os

import torch
from torchmetrics.functional import pairwise_cosine_similarity
from torch.nn.functional import one_hot
from torch.linalg import norm
torch.set_printoptions(precision=2)

from avalanche.training.plugins import SupervisedPlugin
from avalanche.training.templates.supervised import SupervisedTemplate
from avalanche.training.plugins.evaluation import default_evaluator
from avalanche.models import FeatureExtractorBackbone


class CLP(SupervisedTemplate):
    """Continually Learning Prototypes.
    """

    def __init__(
            self,
            clvq_model,
            n_protos,
            bmu_metric,
            criterion,
            alpha_start,
            sim_th,
            input_size,
            num_classes,
            max_allowed_mistakes,
            k_hit=0.7,
            k_miss=3,
            sim_th_tau_crr=100,
            sim_th_tau_err=40,
            eps=0.03,
            verbose = 0,
            output_layer_name=None,
            train_epochs: int = 1,
            train_mb_size: int = 1,
            eval_mb_size: int = 1,
            device="cpu",
            plugins: Optional[Sequence["SupervisedPlugin"]] = None,
            evaluator=default_evaluator,
            eval_every=-1,
            ext_feat=True
    ):
        """Init function for the CLP model.

        :param clp_model: a PyTorch model
        :param criterion: loss function
        :param output_layer_name: if not None, wrap model to retrieve
            only the `output_layer_name` output. If None, the strategy
            assumes that the model already produces a valid output.
            You can use `FeatureExtractorBackbone` class to create your custom
            SLDA-compatible model.
        :param input_size: feature dimension
        :param num_classes: number of total classes in stream
        :param train_mb_size: batch size for feature extractor during
            training. Fit will be called on a single pattern at a time.
        :param eval_mb_size: batch size for inference
        :param shrinkage_param: value of the shrinkage parameter
        :param streaming_update_sigma: True if sigma is plastic else False
            feature extraction in `self.feature_extraction_wrapper`.
        :param plugins: list of StrategyPlugins
        :param evaluator: Evaluation Plugin instance
        :param eval_every: run eval every `eval_every` epochs.
            See `BaseTemplate` for details.
        """

        if plugins is None:
            plugins = []
        
        self.ext_feat = ext_feat
        
        clvq_model.eval()
            
        if output_layer_name is not None:
            clvq_model = FeatureExtractorBackbone(
                    clvq_model.to(device), output_layer_name
            ).eval()

        super().__init__(
                clvq_model,
                None,
                criterion,
                train_mb_size,
                train_epochs,
                eval_mb_size,
                device=device,
                plugins=plugins,
                evaluator=evaluator,
                eval_every=eval_every,
        )
        
        self.device = device
        
        
        # CLVQ parameters
        self.bmu_metric = bmu_metric
        self.n_protos = n_protos
        self.alpha_start = alpha_start
        self.max_allowed_mistakes = max_allowed_mistakes
        self.num_classes = num_classes
        self.verbose = verbose
        self.k_hit = k_hit
        self.k_miss = k_miss
        self.sim_th_tau_crr = sim_th_tau_crr
        self.sim_th_tau_err = sim_th_tau_err
        self.eps = eps
        
        # setup weights for CLVQ
        self.prototypes = 100*torch.ones(n_protos, input_size).to(self.device)
        self.proto_labels = num_classes * torch.ones((n_protos, 1), dtype=int)
        self.alphas = self.alpha_start*torch.ones((n_protos, 1)).to(self.device)
        self.sim_th = sim_th*torch.ones((n_protos, 1)).to(self.device)
        self.hits = torch.ones((n_protos, 1)).to(self.device)
        self.misses = torch.ones((n_protos, 1)).to(self.device)
        self.n_alc_bc_miss = 0    # Num of allocated protos because of errors
        self.seen_labels = []
        self.mistaken_proto_inds = [] # inds of protos that made incorrect inferences for current sample

    def forward(self, return_features=False):
        """Compute the model's output given the current mini-batch."""
        self.model.eval()
        if self.ext_feat:         
            feat = self.model(self.mb_x).flatten(start_dim=1, end_dim=-1)
        else:
            feat = self.mb_x.float()
        
        out = one_hot(self.predict(feat), self.num_classes+1).squeeze(1).float().to(self.device)
        
        if return_features:
            return out, feat
        else:
            return out
        
    def training_epoch(self, **kwargs):
        """
        Training epoch.
        :param kwargs:
        :return:
        """
        for _, self.mbatch in enumerate(self.dataloader):
            self._unpack_minibatch()
            self._before_training_iteration(**kwargs)

            self.loss = torch.tensor([1], dtype=float).to(self.device)

            # Forward
            self._before_forward(**kwargs)
            # compute output on entire minibatch
            self.mb_output, feats = self.forward(return_features=True)
            self._after_forward(**kwargs)

            # Loss & Backward
            # self.loss += self._criterion(self.mb_output, self.mb_y)
            self.loss += 1

            # Optimization step
            self._before_update(**kwargs)
            # process one element at a time
            for f, y in zip(feats, self.mb_y):
                # f = f.squeeze()
                self.fit(f, y.unsqueeze(0))
            self._after_update(**kwargs)

            self._after_training_iteration(**kwargs)

    def make_optimizer(self):
        """Empty function.
        Deep SLDA does not need a Pytorch optimizer."""
        pass

    @torch.no_grad()
    def fit(self, x, y):
        """
        Fit the SLDA model to a new sample (x,y).
        :param x: a torch tensor of the input data (must be a vector)
        :param y: a torch tensor of the input label
        :return: None
        """
        n_mistakes = 0
        mistake = False
        y = torch.tensor(int(y))
        x = x / norm(x, 2)
        
        while True:
            
            bmu_ind, max_sim = self.get_best_matching_unit(x)
            bmu_ind = bmu_ind.item()
            max_sim = max_sim.item()
            
            if y.item() not in self.seen_labels:
                self.seen_labels.append(y.item())
                if self.verbose >= 1:
                    print("Novel Label!")
                self._allocate(x, y)
                break
                
            # Novel instance --> Allocate
            # if no winner, because all similarities are below the given threshold, then allocate
            if bmu_ind == -1:
                if self.verbose >= 1:
                    print("Novel Instance!")
                    print(max_sim, bmu_ind)
                self._allocate(x, y)
                self.mistaken_proto_inds = []  # reset mistake buffer
                break
            
            # # Novel label --> Allocate
            # if y not in self.proto_labels:
            #     self._allocate(x, y)
            #     break
                
            # Get the winner prototype
            bmu = self.prototypes[bmu_ind]
            
            # Calculate Error
            if self.bmu_metric == 'euclidean':
                error = x - bmu
                
            elif self.bmu_metric == 'cosine':
                error = x
                
            # print("winner label: ", self.proto_labels[bmu_ind])
            # print("Target label: ", y)
            
            # If winner not assigned to a label, then assign it to
            # the training instance's label
            if self.proto_labels[bmu_ind] == self.num_classes:
                if self.verbose >= 1:
                    print("Unsupervised allocating...")
                mistake = False
                self.proto_labels[bmu_ind] = y
                self.prototypes[bmu_ind] += self.alphas[bmu_ind] * error
                
                # update the threshold towards max_sim-eps
                self.sim_th[bmu_ind] = self.sim_th[bmu_ind] + (1*max_sim - self.sim_th[bmu_ind]) / self.sim_th_tau_crr
                
                self.hits[bmu_ind]+=1
                self.alphas[bmu_ind] = self.misses[bmu_ind]/self.hits[bmu_ind]
                # self._bound_weights(bmu_ind)
                self.mistaken_proto_inds = []
                break

            # Update the winner based on its inference
            # If CORRECT prediction
            elif self.proto_labels[bmu_ind] == y:
                mistake = False
                if self.verbose == 2:
                    print("Correct")
                # print(self.alphas[bmu_ind])
                self.prototypes[bmu_ind] += self.alphas[bmu_ind] * error
                
                # update the threshold towards max_sim-eps
                # self.sim_th[bmu_ind] += 0.3*self.alphas[bmu_ind] * (max_sim - self.sim_th[bmu_ind] - self.eps)
                # self.sim_th[bmu_ind] += 0.008 * (max_sim - self.sim_th[bmu_ind] - self.eps)
                self.sim_th[bmu_ind] = self.sim_th[bmu_ind] + (0.70*max_sim - self.sim_th[bmu_ind]) / self.sim_th_tau_crr             
                
                self.hits[bmu_ind]+=self.k_hit
                self.alphas[bmu_ind] = self.misses[bmu_ind]/self.hits[bmu_ind]
                # self._bound_weights(bmu_ind)
                self.mistaken_proto_inds = []
                break

            # if INCORRECT prediction 
            else:
                # print("Mistake")
                mistake = True
                n_mistakes += 1
                
                self.mistaken_proto_inds.append(bmu_ind)
                self.prototypes[bmu_ind] -= self.alphas[bmu_ind] * error
                
                # update the threshold towards max_sim+eps
                # self.sim_th[bmu_ind] += 4*self.alphas[bmu_ind] * (max_sim - self.sim_th[bmu_ind] + self.eps)
                self.sim_th[bmu_ind] = self.sim_th[bmu_ind] + (1*max_sim - self.sim_th[bmu_ind]) / self.sim_th_tau_err
                
                
                self.misses[bmu_ind] += self.k_miss
                self.alphas[bmu_ind] = self.misses[bmu_ind]/self.hits[bmu_ind]
                
                if self.verbose >= 1:
                    print("Mistaken prototype:", self.proto_labels[bmu_ind].item(), bmu_ind)
                    print("sim_th & alpha:", self.sim_th[bmu_ind].item(), self.alphas[bmu_ind].item())
                
                # If more misses than hits, then forget this prototype, reset it
                if self.alphas[bmu_ind] > 1: 
                    self._forget(bmu_ind)
                
                # Try again if you have not checked all top matches
                if n_mistakes < self.max_allowed_mistakes:
                    # print("First Mistake trying again...")
                    continue

                # Allocate a new non-winning prototype if maximum number of allowed
                # mistakes are passed
                elif n_mistakes == self.max_allowed_mistakes:
                    if self.verbose >= 1:
                        print("Ignoring the instance")
                    self.n_alc_bc_miss += 1
                    self.mistaken_proto_inds = []
                    # self._allocate(x, y)
                    break
    
    def _allocate (self, x, y):
        # print("Mistake again, allocating...")
        similarities = self._calc_similarities(x)
        similarities[self.proto_labels<self.num_classes] -= 100000
        
        bmu_ind = torch.argmax(similarities, dim=0)
        self.proto_labels[bmu_ind] = y
        
        error = x - self.prototypes[bmu_ind]
        self.prototypes[bmu_ind] += self.alphas[bmu_ind] * error
            
        self.hits[bmu_ind]+=1
        self.alphas[bmu_ind] = self.misses[bmu_ind]/self.hits[bmu_ind]
        # self._bound_weights(bmu_ind)
    
    def _forget(self, proto_ind):
        self.proto_labels[proto_ind] = self.num_classes
        self.alphas[proto_ind] = self.alpha_start
        self.hits[proto_ind], self.misses[proto_ind] = 1, 1
        
    def memory_cleanup(self, alpha_th=0.5):
        pt_inds_to_clean = (self.alphas >= alpha_th)
        self.proto_labels[pt_inds_to_clean] = self.num_classes
        self.alphas[pt_inds_to_clean] = self.alpha_start
        self.hits[pt_inds_to_clean], self.misses[pt_inds_to_clean] = 1, 1
        
    @torch.no_grad()
    def predict(self, x):
        """
        Make predictions on test data X.
        :param X: a torch tensor that contains N data samples (N x d)
        :param return_probas: True if the user would like probabilities instead
        of predictions returned
        :return: the test predictions or probabilities
        """

        # Compute the winner prototype, return this and its index
        bmu_inds, _ = self.get_best_matching_unit(x) 
        # Find the predicted labels
        preds = self.proto_labels[bmu_inds]
        # Infer "Unknown Instance" label (as label == n_classes) as predictions
        preds[bmu_inds==-1] = self.num_classes
        
        # return predictions
        return preds

    # Locate the best matching unit
    def get_best_matching_unit(self, x):

        ind = 0
        
        similarities = self._calc_similarities(x)
        similarities[self.mistaken_proto_inds] -= 10000
        sims = similarities.clone().detach()
        
        th_passing_check  = torch.gt(sims, self.sim_th.tile((1, sims.shape[1])))
        sims_sorted, inds_sorted = torch.sort(sims, 0, descending=True)
        th_passing_sorted = torch.gather(th_passing_check, 0, inds_sorted)
        
        bmu_inds = torch.zeros(size=(1, sims.shape[1]))
        max_sims = torch.zeros(size=(1, sims.shape[1]))
        
        for i in range(sims.shape[1]):
            top_th_passing_inds = inds_sorted[th_passing_sorted[:,i],i]
            max_th_passing_sims = sims_sorted[th_passing_sorted[:,i],i]
            if len(top_th_passing_inds) > 0:
                bmu_inds[0,i] = top_th_passing_inds[0]
                max_sims[0,i] = max_th_passing_sims[0]
            else:
                bmu_inds[0,i] = -1
                max_sims[0,i] = 0
        
#         top_sims, top_inds = torch.sort(sims, 0, descending=True)
#         th_passing_check, max_th_passing_inds  = torch.max(torch.gt(top_sims, self.sim_th[top_inds].squeeze(2)), dim=0, keepdim=True)
        
#         bmu_inds = torch.gather(top_inds, 0, max_th_passing_inds.T)
#         max_sim = torch.gather(similarities, 0, bmu_inds).squeeze()
        
#         # Unknown
#         bmu_inds[th_passing_check.squeeze()==False] = -1
#         bmu_inds = bmu_inds.squeeze()
        inds_sorted = inds_sorted.long()
        top_sims, top_inds = sims_sorted[:5,:], inds_sorted[:5,:]
        # print(top_sims.shape, top_inds.shape)
        if self.verbose == 2:
            for i in range(0,top_sims.shape[1], 3): 
                print("-----------------------------------------------------------")
                print("sims:  ",top_sims[:,i].t().data)
                print("simth: ",self.sim_th[top_inds[:,i]].t().data)
                print("labels:",self.proto_labels[top_inds[:,i]].t().data)
                print("alphas:",self.alphas[top_inds[:,i]].t().data)
        
        bmu_inds = bmu_inds.squeeze()
        max_sims = max_sims.squeeze()
        
        # (max_sim, bmu_inds) = torch.max(similarities, dim=0, keepdim=False)
        # bmu_inds[torch.gt(self.sim_th[bmu_inds].flatten(), max_sim)==True] = -1
        return bmu_inds.long(), max_sims
            
#         if self.bmu_metric == 'euclidean':
#             euc_dist = torch.cdist(self.prototypes, x, p=2)
#             (min_dist, ind) = torch.min(euc_dist, dim=0, keepdim=False)
#             self.sims.append(min_dist)
#             if torch.count_nonzero(min_dist > self.sim_th) > 0:
#                 return None, None
            
#         elif self.bmu_metric == 'dot_product':
#             dp = torch.mm(self.prototypes, x.T)
#             if torch.count_nonzero(dp>self.sim_th) > 0:
#                 ind = torch.argmax(dp, dim=0)
#                 self.sims.append(torch.max(dp, dim=0))
#             else:
#                 return None, None
            
#         elif self.bmu_metric == 'cosine':
#             ind = torch.argmax(pairwise_cosine_similarity(self.prototypes, x),
#                                dim=0)
        
        
    
    def _calc_similarities(self, x):
        
        similarities = 0
        
        if len(x.shape) == 1:
            x = x.unsqueeze(dim=0)

        if self.bmu_metric == 'euclidean':
            similarities = -torch.cdist(self.prototypes, x, p=2)
            
        elif self.bmu_metric == 'dot_product':
            similarities = torch.mm(self.prototypes, x.T)          
            
        elif self.bmu_metric == 'cosine':
            similarities = pairwise_cosine_similarity(self.prototypes, x)
            
        return similarities

    def init_prototypes_from_data(self, data):
        
        num_bins = 100
        x = data[0,:].cpu()
        x[np.absolute(x)<0.02]=0

        counts, bins = np.histogram(x, bins=num_bins)
        bins = bins[:-1]
        probs = counts/float(counts.sum())
        
        self.prototypes = torch.tensor(np.random.choice(bins, size=(self.n_protos, data.shape[1]), replace=True, p=probs)).to(self.device)
        # torch.abs(torch.round(
        #     torch.normal(mean=torch.mean(data, axis=0).tile(self.n_protos, 1),
        #                  std=torch.std(data, axis=0).tile(self.n_protos, 1),
        #                  out=self.prototypes), decimals=1))

        return self.prototypes
    
    def criterion(self):
        """Loss function."""
        return 0

