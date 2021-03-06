# -*- coding: utf-8 -*-
"""
Created on Mon Aug 07 11:44:53 2017

Minibatch Importance Sampled Mixture of Experts (ISMOE) code
from "Embarassingly parallel inference for Gaussian processes"
by M. M. Zhang and S. A. Williamson.

@author: Michael Zhang
"""
# Python 2 to 3 conversion
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from past.builtins import xrange
from past.builtins import map
import numpy as np
import GPy
import time
#import pdb
from mpi4py import MPI
from scipy import stats
from scipy.misc import logsumexp
from scipy.sparse import lil_matrix
from itertools import product
from scipy.optimize import minimize, brent
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import LabelEncoder as LE
from sklearn.cluster import KMeans
from sklearn.mixture import BayesianGaussianMixture
from sklearn.model_selection import KFold
from GPy.util.univariate_Gaussian import std_norm_cdf
from GPy.util.linalg import jitchol, dtrtrs

def bernoulli_d2logpdf_dlink2(f, y):
    inv_link_f= std_norm_cdf(f)
    arg = np.where(y==1, inv_link_f, 1.-inv_link_f)
    ret =  -1./np.square(np.clip(arg, 1e-9, 1e9))
    if np.any(np.isinf(ret)):
        raise
    return ret

def bernoulli_dlogpdf_dlink(f, y):
    inv_link_f= std_norm_cdf(f)
    ff = np.clip(inv_link_f, 1e-9, 1-1e-9)
    denom = np.where(y==1, ff, -(1-ff))
    return 1./denom

def bernoulli_logpdf(f, y):
    inv_link_f= std_norm_cdf(f)
    p = np.where(y==1, inv_link_f, 1.-inv_link_f)
    return np.log(np.clip(p, 1e-9 ,np.inf))

def _compute_B_statistics(K, W):
    if np.any(np.isnan(W)):
        raise ValueError('One or more element(s) of W is NaN')
    W_12 = np.sqrt(W)
    B = np.eye(K.shape[0]) + W_12*K*W_12.T
    L = jitchol(B)
    LiW12, _ = dtrtrs(L, np.diagflat(W_12), lower=1, trans=0)
    K_Wi_i = np.dot(LiW12.T, LiW12) # R = W12BiW12, in R&W p 126, eq 5.25
    C = np.dot(LiW12, K)
    Ki_W_i = K - C.T.dot(C)
    I_KW_i = np.eye(K.shape[0]) - np.dot(K, K_Wi_i)
    logdet_I_KW = 2*np.sum(np.log(np.diag(L)))
    return K_Wi_i, logdet_I_KW, I_KW_i, Ki_W_i

def _unscaled_dist(X, X2=None):
    if X2 is None:
        Xsq = np.sum(np.square(X),1)
        r2 = -2.*GPy.util.linalg.tdot(X) + (Xsq[:,None] + Xsq[None,:])
        GPy.util.diag.view(r2)[:,]= 0. # force diagnoal to be zero: sometime numerically a little negative
        r2 = np.clip(r2, 0, np.inf)
        return np.sqrt(r2)
    else:
        X1sq = np.sum(np.square(X),1)
        X2sq = np.sum(np.square(X2),1)
        r2 = -2.*np.dot(X, X2.T) + X1sq[:,None] + X2sq[None,:]
        r2 = np.clip(r2, 0, np.inf)
        return np.sqrt(r2)

class ISMOE(object):
    def __init__(self,X, Y, X_star, Y_star, K=50, alpha=500.,J=2,
                 classification=False,partition="gmm", IS=True,
                 stationary=False, N_minibatch = 1000, full_cov=True,
                 mb_upweight=True):
        """
        ISMOE code for classification and regression.
        Parameters:
            X: N x D Numpy array of training inputs.
            Y: N x 1 Numpy array of training outputs
            X_star: N_star x D Numpy array of test inputs
            Y_star N_star x 1 Numpy array of test outputs
            K: Integer, number of cluster components per importance sample
            alpha: Float, concentration parameter of Dirichlet mixture model
            J: Integer, number of importance samples
            classification: Bool, True to run classification and False
                            for regression
            partition: String, partition types. Availble options are "gmm",
                        "kmeans", "random", and "vi"
            IS: Bool, weighting type. True for importance weights, False for
                uniform weights.
            Stationary: Bool, True for fitting a stationary kernel, False for
                        Non-stationary
            N_minibatch: Integer, Size of stochastic approximation, must be
                         less than N. Ignored for classification.
            full_cov: Bool, True if you want full covariance matrix returned in
                      predictions. False if you want just the diagonal.
            mb_upweight: Bool, True if you want to upweight likelihood for
                         stochastic approximation. Ignored for classifcation.
        """
        self.start_time = time.time()
        self.comm = MPI.COMM_WORLD
        self.P = self.comm.Get_size()
        self.rank = self.comm.Get_rank()
        self.total_J = int(J)
        self.full_cov = bool(full_cov)
        self.proposal_sizes = [j.size for j in np.array_split(xrange(self.total_J), self.P)]
        assert(len(self.proposal_sizes) == self.P)
        assert(sum(self.proposal_sizes) == self.total_J)
        self.classification = bool(classification)
        self.partition = str(partition)
        self.IS = bool(IS)
        assert(self.partition == "random" or self.partition == "gmm" or self.partition == "kmeans" or self.partition =="vi")
        if self.classification:
            self.stationary = True
        else:
            self.stationary=bool(stationary)

        self.J = self.proposal_sizes[self.rank]
        self.K = int(K)
        self.alpha=alpha
        self.X = X
        self.Y = np.array(Y).reshape(-1,1)
        (self.N, self.D) = self.X.shape
#        if self.classification:
#            self.N_minibatch = self.N
#        else:
#            self.N_minibatch = N_minibatch
        self.N_minibatch = N_minibatch

        assert(self.N_minibatch <= self.N)


        if mb_upweight:
            self.mb_weight = float(self.N) / float(self.N_minibatch)
        else:
            self.mb_weight = 1.
        assert(self.N == self.Y.size)
        self.U = np.array([np.random.choice(self.N,size=self.N_minibatch,replace=False)for j in xrange(self.J)] )

        self.X_mean = self.X.mean(axis=0)
        self.X_sd = np.sqrt(self.X.var(axis=0))
        self.X_star = X_star
        self.Y_star = np.array(Y_star).reshape(-1,1)
        (self.N_star, D_star) = self.X_star.shape
        assert(D_star == self.D)
        self.Z_proposal = np.random.choice(self.K,size=(self.J,self.N_minibatch)).astype(int)
        self.Z_star = np.random.choice(self.K,size=(self.J,self.N_star)).astype(int)
        self.proposal_prob = np.zeros(self.J)
        self.Y_star_LL = np.zeros(self.J)
        self.Y_star_score = np.zeros(self.J)
        self.Y_star_var = np.empty((self.J, self.N_star))
        if self.full_cov:
            self.Y_star_cov = {j: lil_matrix((self.N_star,self.N_star)) for j in xrange(self.J)}
        else:
            self.Y_star_cov = {j: lil_matrix((self.N_star,1)) for j in xrange(self.J)}
        self.Y_star_predict = np.empty((self.J, self.N_star))

        if self.K > 1:
            Z_map = map(self.partition_data,product([self.partition],xrange(self.J)))
            self.Z_proposal = np.array([Z for Z,Z_star in Z_map]).reshape(self.J,-1)
            self.Z_star = np.array([Z_star for Z,Z_star in Z_map]).reshape(self.J,-1)

        self.models = map(self.model_init,xrange(self.J))
        self.comm.barrier()
#        if self.classification:
#            self.model_update()

    def partition_data(self,args):
        method, j = args
        if method== "vi":
            dp = BayesianGaussianMixture(n_components = self.K,weight_concentration_prior = self.alpha, max_iter=1,init_params='kmeans',weight_concentration_prior_type='dirichlet_process')
            dp.fit(self.X[self.U[j]])
            Z = dp.predict(self.X[self.U[j]]).astype(int)
            Z_star = dp.predict(self.X_star).astype(int)
        if method=="gmm":
            Z,Z_star= self.uncollapsed_dp_partition_alt(j)
        elif method=="kmean":
            km = KMeans(n_clusters=self.K)
            Z = km.fit_predict(self.X[self.U[j]]).astype(int)
            Z_star = km.predict(self.X_star[self.U[j]]).astype(int)
        else:
            Z = np.random.choice(self.K,size = self.N_minibatch,replace=True)
            Z_star = np.random.choice(np.unique(Z),size = self.N_star,replace=True)
        le = LE()
        le.fit(np.hstack((Z,Z_star)))
        Z = le.transform(Z)
        Z_star = le.transform(Z_star)
        if (method=="vi"): #& (self.vi_partition):
            Z_diff = np.setdiff1d(Z_star,Z)
            if Z_diff.size > 0:
                idx = np.hstack((np.where(Z_star==k) for k in Z_diff)).flatten()
                unique_Z = np.unique(Z)
                post_Z = dp.predict_proba(self.X_star[idx])[:,unique_Z]
                Z_star[idx] = [np.random.choice(unique_Z,p = post_Z_i / post_Z_i.sum() ) for post_Z_i in post_Z]
                assert(np.setdiff1d(Z_star,Z).size == 0)
        return(Z,Z_star)

    def uncollapsed_dp_partition_alt(self,j):
        mu_k = np.random.normal(self.X_mean, self.X_sd, size = (self.K,self.D))
#        sigma_k = np.random.gamma(.5,2,size=(self.K, self.D)) #could just have fixed
        sigma_k = np.eye(self.D)
        pi_j = np.random.dirichlet([self.alpha]*self.K)
        ll = np.tile(np.log(pi_j),(self.N_minibatch,1))
        for k in xrange(self.K):
            ll[:,k] += stats.multivariate_normal.logpdf(self.X[self.U[j]], mu_k[k,:], cov=sigma_k, allow_singular=True) #much more efficient than doing one by one in general
        Z = np.array([ np.random.multinomial(1,np.exp(ll[n,:]-logsumexp(ll[n,:]))).argmax() for n in xrange(self.N_minibatch)])
        Z_count =  np.bincount(Z, minlength=self.K)
        unique_Z = np.unique(Z)
        Z_dict= {idx:z for idx,z in enumerate(unique_Z)}
        non_zero_Z = Z_count.nonzero()
        collapsed_pi = np.log(Z_count[non_zero_Z] + self.alpha) - np.log(self.N_minibatch + self.alpha)
        ll_star = np.tile(collapsed_pi,(self.N_star,1))
        for idx,k in enumerate(unique_Z):
            ll_star[:,idx] += stats.multivariate_normal.logpdf(self.X_star, mu_k[k,:], cov=sigma_k,allow_singular=True)
        Z_star = np.array([ Z_dict[np.random.multinomial(1,np.exp(ll_star[n,:]-logsumexp(ll_star[n,:]))).argmax()] for n in xrange(self.N_star)])
        return(Z,Z_star)

    def model_init(self,j):
        init_n = 10 # initialize hyperparameters with a tiny subset of minibatch
        unique_Z_j = np.unique(self.Z_proposal[j])
        gp_model = {}
        norm = {k:_unscaled_dist(self.X[self.U[j]][self.Z_proposal[j]==k]) for k in unique_Z_j}
        if self.classification:
            if self.N_minibatch > init_n:
                choose_init =  np.random.choice(self.N_minibatch,size=init_n,replace=False)
                m0_flag = True
                m0 = GPy.core.GP(self.X[self.U[j]][choose_init], self.Y[self.U[j]][choose_init].reshape(-1,1), kernel = GPy.kern.RBF(self.D),
                                inference_method=GPy.inference.latent_function_inference.laplace.Laplace(),
                                likelihood=GPy.likelihoods.Bernoulli())
                while m0_flag: # sometimes initial choice returns cholesky error
                    try:
                        m0.optimize()
                        m0_flag=False
                    except:
                        choose_init =  np.random.choice(self.N_minibatch,size=init_n,replace=False)
                        m0 = GPy.core.GP(self.X[self.U[j]][choose_init], self.Y[self.U[j]][choose_init].reshape(-1,1), kernel = GPy.kern.RBF(self.D),
                                inference_method=GPy.inference.latent_function_inference.laplace.Laplace(),
                                likelihood=GPy.likelihoods.Bernoulli())
                init_hyp = np.log(m0.param_array)
                del m0
            else:
                init_hyp = np.ones(2)
            min_f = minimize(self.stationary_objective_fun_class,init_hyp, args=(j,), method='L-BFGS-B')
            hyp = np.exp(min_f.x)
            self.proposal_prob[j] = sum([self.calc_class_marg_LL(hyp,j,k,norm[k]) for k in unique_Z_j])
            assert(~np.isnan(self.proposal_prob[j]))

        else:
            if self.N_minibatch > init_n:
                choose_init =  np.random.choice(self.N_minibatch,size=init_n,replace=False)
                m0_flag = True
                m0 = GPy.models.GPRegression(self.X[self.U[j]][choose_init], self.Y[self.U[j]][choose_init].reshape(-1,1), kernel = GPy.kern.RBF(self.D))
                while m0_flag: # sometimes initial choice returns cholesky error
                    try:
                        m0.optimize()
                        m0_flag=False
                    except:
                        choose_init =  np.random.choice(self.N_minibatch,size=init_n,replace=False)
                        m0 = GPy.models.GPRegression(self.X[self.U[j]][choose_init], self.Y[self.U[j]][choose_init].reshape(-1,1), kernel = GPy.kern.RBF(self.D))

                init_hyp = np.log(m0.param_array)
                del m0
            else:
                init_hyp = np.ones(3)

            if self.stationary:
                min_f = minimize(self.stationary_objective_fun_2,init_hyp, args=(j,norm), method='L-BFGS-B')
                hyp = np.exp(min_f.x)
                if self.mb_weight > 1.:
                    self.proposal_prob[j] = -1.*min_f.fun


        for k in unique_Z_j:
            K_mask = (self.Z_proposal[j]==k)
            Y_k = np.copy(self.Y[self.U[j]][K_mask]).reshape(-1,1)
            X_k = np.copy(self.X[self.U[j]][K_mask])
############################ start classification ############################################
            if self.classification:
                gp_model[k] = GPy.core.GP(X_k,Y_k, kernel =  GPy.kern.RBF(self.D, lengthscale=init_hyp[0],variance=init_hyp[1]) ,likelihood=GPy.likelihoods.Bernoulli(),inference_method=GPy.inference.latent_function_inference.laplace.Laplace())
######################################## end classification #############################################

######################################## start regression ##################################
            else:
                if self.stationary:
                    gp_model[k] = GPy.models.GPRegression(X_k,Y_k, kernel = GPy.kern.RBF(self.D, lengthscale=hyp[0],variance=hyp[1]), noise_var=hyp[2]/self.mb_weight)
                    assert(~np.isnan(gp_model[k].log_likelihood()))
                    if self.mb_weight == 1:
                        self.proposal_prob[j] += gp_model[k].log_likelihood()

                else:
                    min_f=minimize(self.neg_log_marg_LL_ns,init_hyp, args=(j,k,norm))
                    hyp = np.exp(min_f.x)
                    gp_model[k] = GPy.models.GPRegression(X_k,Y_k, kernel = GPy.kern.RBF(self.D, lengthscale=hyp[0],variance=hyp[1]), noise_var=hyp[2])
                    if self.mb_weight == 1:
                        self.proposal_prob[j] += gp_model[k].log_likelihood()
                    else:
                        self.proposal_prob[j] += -1.*min_f.fun
######################################## end regression ###################################
            K_star_mask = (self.Z_star[j] == k)
            assert(K_star_mask.size == self.N_star)
            X_star_k = self.X_star[K_star_mask]
            if self.full_cov:
                sparse_cov = lil_matrix((self.N_star,self.N_star))
            else:
                sparse_cov = lil_matrix((self.N_star,1))
            pred_GP_mean, pred_GP_cov = gp_model[k].predict(X_star_k,full_cov=self.full_cov)
            if self.full_cov:
                for idx,x_k in enumerate(np.where(self.Z_star[j]==k)[0]):
                    sparse_cov[x_k,K_star_mask] = np.copy(pred_GP_cov[idx,:]).reshape(1,-1)
                    sparse_cov[K_star_mask,x_k] = np.copy(pred_GP_cov[idx,:]).reshape(-1,1)
            else:
                sparse_cov[K_star_mask] = np.copy(pred_GP_cov)
                self.Y_star_cov[j] += sparse_cov

                if self.classification:
                    self.Y_star_LL[j] += np.log(np.clip(np.where(self.Y_star.flatten()[K_star_mask]==1,self.Y_star_predict[j][K_star_mask].flatten(),1.-self.Y_star_predict[j][K_star_mask].flatten()), 1e-9 ,np.inf)).sum()
                else:
                    if self.full_cov:
                        self.Y_star_LL[j] += stats.norm.logpdf(self.Y_star.flatten()[K_star_mask],self.Y_star_predict[j][K_star_mask].flatten(), np.sqrt(np.diag(self.Y_star_cov[j].toarray()))[K_star_mask].flatten()).sum()
                    else:
                        self.Y_star_LL[j] += stats.norm.logpdf(self.Y_star.flatten()[K_star_mask],self.Y_star_predict[j][K_star_mask].flatten(), np.sqrt(self.Y_star_cov[j][K_star_mask].toarray()).flatten()).sum()

            self.Y_star_predict[j][K_star_mask] = pred_GP_mean.flatten()
            gp_model[k] = None
        return(gp_model)

#    def model_update(self): # only runs for classification
#        for j in xrange(self.J):
#            unique_Z_j = np.unique(self.Z_proposal[j])
#            if self.stationary:
#                w,h = self.proposal_weight_calc_stationary(j)
#                self.proposal_prob[j] = w
#                for k in unique_Z_j:
#                    self.models[j][k].optimizer_array = h
#            else:
#                model_fit_out = map(self.proposal_weight_calc, zip([j]*unique_Z_j.size,unique_Z_j))
#                for w,h,k in model_fit_out:
#                    self.proposal_prob[j] += w
#                    self.models[j][k].optimizer_array = h
#                    self.models[j][k].set_state(h)
#            non_zero_K_star = np.unique(self.Z_star[j])
#            pred_output = map(self.predictive_fit, zip([j]*non_zero_K_star.size, non_zero_K_star))
#            if np.isinf(sum([out[2] for out in pred_output])):
#                self.proposal_prob[j] = -np.inf
#            else:
#                self.Y_star_predict[j] = np.sum([out[0] for out in pred_output],axis=0).toarray().flatten()
#                self.Y_star_cov[j] = np.sum([out[1] for out in pred_output],axis=0)
#                if self.classification:
#                    self.Y_star_LL[j] = np.clip(np.where(self.Y_star.flatten()==1,self.Y_star_predict[j].flatten(),1.-self.Y_star_predict[j].flatten()), 1e-9 ,np.inf)
#                else:
#                    self.Y_star_LL[j] = stats.norm.logpdf(self.Y_star.flatten(),self.Y_star_predict[j].flatten(), np.sqrt(np.diag(self.Y_star_cov[j].toarray())).flatten()).sum()
#            if self.classification:
#                self.Y_star_score[j] = roc_auc_score(self.Y_star.flatten(), (np.exp(self.Y_star_predict[j]) / (1. + np.exp(self.Y_star_predict[j]))).flatten())
#            else:
#                self.Y_star_score[j] = np.mean((self.Y_star.flatten() - self.Y_star_predict[j].flatten())**2)

    def calc_class_marg_LL(self,hyp,j, k,norm_k): # calculate laplace approx. marginal likelihood for classification
#        hyp = np.exp(hyp)
    # hyp lives on positive reals
        K_mask = (self.Z_proposal[j]==k)
        Y_k = self.Y[self.U[j]][K_mask].reshape(-1,1)
        K =  hyp[1]*np.exp(  -.5 * (norm_k/ hyp[0] )**2)
        f_hat, Ki_f_hat = self.rasm_mode(K,Y_k)
        return(self.mode_computations(f_hat, Ki_f_hat, K,Y_k))
#        return(LL)

    def rasm_mode(self,K, Y):
        Ki_f = np.zeros_like(Y)
        f = np.dot(K, Ki_f)
        _mode_finding_tolerance = 1e-4
        _mode_finding_max_iter = 30

        #define the objective function (to be maximised)
        def obj(Ki_f, f):
            ll = -0.5*np.sum(np.dot(Ki_f.T, f)) +   self.mb_weight* np.sum(bernoulli_logpdf(f, Y))
            if np.isnan(ll):
#                import ipdb; ipdb.set_trace()  # XXX BREAKPOINT
                return -np.inf
            else:
                return ll


        difference = np.inf
        iteration = 0
        while difference > _mode_finding_tolerance and iteration < _mode_finding_max_iter:
            W = -self.mb_weight*bernoulli_d2logpdf_dlink2(f, Y)
            if np.any(np.isnan(W)):
                raise ValueError('One or more element(s) of W is NaN')
            grad = self.mb_weight*bernoulli_dlogpdf_dlink(f, Y)
            if np.any(np.isnan(grad)):
                raise ValueError('One or more element(s) of grad is NaN')

            W_f = W*f

            b = W_f + grad # R+W p46 line 6.
            W12BiW12, _, _, _ = _compute_B_statistics(K, W)
            W12BiW12Kb = np.dot(W12BiW12, np.dot(K, b))

            #Work out the DIRECTION that we want to move in, but don't choose the stepsize yet
            full_step_Ki_f = b - W12BiW12Kb # full_step_Ki_f = a in R&W p46 line 6.
            dKi_f = full_step_Ki_f - Ki_f

            #define an objective for the line search (minimize this one)
            def inner_obj(step_size):
                Ki_f_trial = Ki_f + step_size*dKi_f
                f_trial = np.dot(K, Ki_f_trial)
                return -obj(Ki_f_trial, f_trial)

            #use scipy for the line search, the compute new values of f, Ki_f
            step = brent(inner_obj, tol=1e-4, maxiter=12)
            Ki_f_new = Ki_f + step*dKi_f
            f_new = np.dot(K, Ki_f_new)
            #print "new {} vs old {}".format(obj(Ki_f_new, f_new), obj(Ki_f, f))
            old_obj = obj(Ki_f, f)
            new_obj = obj(Ki_f_new, f_new)
            if new_obj < old_obj:
                raise ValueError("Shouldn't happen, brent optimization failing")
            difference = np.abs(new_obj - old_obj)
            # difference = np.abs(np.sum(f_new - f)) + np.abs(np.sum(Ki_f_new - Ki_f))
            Ki_f = Ki_f_new
            f = f_new
            iteration += 1
    #    log_marginal, woodbury_inv, dL_dK, dL_dthetaL = self.mode_computations(f_hat, Ki_fhat, K, Y, likelihood, kern, Y_metadata)
        return f, Ki_f

    def mode_computations(self, f_hat, Ki_f, K, Y):
        """
        At the mode, compute the hessian and effective covariance matrix.

        returns: logZ : approximation to the marginal likelihood
                 woodbury_inv : variable required for calculating the approximation to the covariance matrix
                 dL_dthetaL : array of derivatives (1 x num_kernel_params)
                 dL_dthetaL : array of derivatives (1 x num_likelihood_params)
        """
        #At this point get the hessian matrix (or vector as W is diagonal)
        W = -self.mb_weight*bernoulli_d2logpdf_dlink2(f_hat, Y)
        if np.any(np.isnan(W)):
            raise ValueError('One or more element(s) of W is NaN')

        _, logdet_I_KW, _, _ = _compute_B_statistics(K, W)

        #compute the log marginal
        log_marginal = -0.5*np.sum(np.dot(Ki_f.T, f_hat)) + self.mb_weight* np.sum(bernoulli_logpdf(f_hat, Y)) - 0.5*logdet_I_KW

        return log_marginal

#    def proposal_weight_calc(self,args):
#        j,k = args
#        try:
#            m_opt = self.models[j][k].optimize()
#            weight = -m_opt.f_opt # subtract neg. LL
#            hyp = m_opt.x_opt
#        except:
#            weight = -np.inf # if optimization throws error, set importance weight to zero
#            if self.classification:
#                hyp = np.array([1,1])
#            else:
#                hyp = np.array([1,1,1])
#        return(weight, hyp, k)

#    def proposal_weight_calc_stationary(self,j):
#        init_hyp = np.log(self.models[j][0].param_array)
#        m_opt = minimize(self.stationary_objective_fun, init_hyp, jac=self.grad_stationary_objective_fun, args=(j,), method='L-BFGS-B')
#        weight = -m_opt.fun  # subtract neg. LL
#        hyp = m_opt.x
#        return(weight, hyp)

    def stationary_objective_fun_class(self,hyp,j):
        hyp = np.exp(hyp)
        LL = 0
        for idx,k in enumerate(np.unique(self.Z_proposal[j])):
            K_mask = (self.Z_proposal[j]==k)
            Y_k = np.copy(self.Y[self.U[j]][K_mask]).reshape(-1,1)
            X_k = np.copy(self.X[self.U[j]][K_mask])
            try:
                if idx is 0:
                    gp_model_k = GPy.core.GP(X_k,Y_k, kernel =  GPy.kern.RBF(self.D, lengthscale=hyp[0],variance=hyp[1]) ,likelihood=GPy.likelihoods.Bernoulli(),inference_method=GPy.inference.latent_function_inference.laplace.Laplace())
                else:
                    gp_model_k.set_XY(X=X_k,Y=Y_k)
                LL += gp_model_k.objective_function()
            except:
                return(np.inf)
        return(LL)


    def stationary_objective_fun_2(self,hyp,j, norm):
        hyp = np.exp(hyp)
        LL = 0
        for k in np.unique(self.Z_proposal[j]):
            K_mask = (self.Z_proposal[j]==k)
            Y_k = self.Y[self.U[j]][K_mask].reshape(-1,1)
            N_k = Y_k.size
            if N_k > 1:
                kernel_k = hyp[1]*np.exp(  -.5 * (norm[k]/ hyp[0] )**2)
                kernel_k += (hyp[2]/(self.mb_weight)  + 1e-6)*np.eye(N_k)
                try:
                    Wi, LW, LWi, W_logdet = GPy.util.linalg.pdinv(kernel_k)
                    alpha, _ = GPy.util.linalg.dpotrs(LW, Y_k, lower=1)
                    LL +=  0.5*(-N_k*self.mb_weight * np.log(2.*np.pi) -  W_logdet - np.sum(alpha * Y_k))
                except:
                    return(np.inf)
            else:
                kernel_k = (hyp[2]/(self.mb_weight)  + hyp[1])
                LL += 0.5*(-N_k*self.mb_weight*  np.log(2.*np.pi) - np.log(kernel_k) - (Y_k[0]**2 / kernel_k))
        return(-1.*LL)

#    def stationary_objective_fun(self,hyp,j):
#        neg_LL = sum(map(self.stationary_LL_calc,zip([hyp]*len(self.models[j]),self.models[j], [j]*len(self.models[j]))))
#        return(neg_LL)
#
#    def grad_stationary_objective_fun(self,hyp,j):
#        grad_hyp = sum(map(self.grad_stationary_LL_calc,zip([hyp]*len(self.models[j]),self.models[j], [j]*len(self.models[j]))))
#        return(grad_hyp)
#
#    def grad_stationary_LL_calc(self,args):
#        hyp,m,j = args
#        self.models[j][m].optimizer_array = hyp
#        return(self.models[j][m].objective_function_gradients())
#
#    def stationary_LL_calc(self,args):
#        hyp,m,j = args
#        self.models[j][m].optimizer_array = hyp
#        return(self.models[j][m].objective_function())

    def neg_log_marg_LL_ns(self,hyp,j,k,norm): # hyp is [log lengthscale, log amplitude, log gaussian noise]
        hyp = np.exp(hyp)
        K_mask = (self.Z_proposal[j]==k)
        Y_k = self.Y[self.U[j]][K_mask].reshape(-1,1)
        N_k = Y_k.size
        if N_k > 1:
            kernel_k = hyp[1]*np.exp(  -.5 * (norm[k]/ hyp[0] )**2)
            kernel_k += (hyp[2]/(self.mb_weight)  + 1e-6)*np.eye(N_k)
            try:
                Wi, LW, LWi, W_logdet = GPy.util.linalg.pdinv(kernel_k)
                alpha, _ = GPy.util.linalg.dpotrs(LW, Y_k, lower=1)
                LL =  0.5*(-N_k*self.mb_weight * np.log(2.*np.pi) -  W_logdet - np.sum(alpha * Y_k))
            except:
                return(np.inf)
        else:
            kernel_k = (hyp[2]/(self.mb_weight)  + hyp[1])

            LL = 0.5*(-N_k*self.mb_weight*  np.log(2.*np.pi) - np.log(kernel_k) - (Y_k[0]**2 / kernel_k))
        return(-1.*LL)

#    def predictive_fit(self, args):
#        j,k = args
#        K_star_mask = (self.Z_star[j] == k)
#        assert(K_star_mask.size == self.N_star)
#        X_star_k = self.X_star[K_star_mask]
#        sparse_mean = lil_matrix((self.N_star,1))
#        if self.full_cov:
#            sparse_cov = lil_matrix((self.N_star,self.N_star))
#        else:
#            sparse_cov = lil_matrix((self.N_star,1))
#        pred_GP_mean, pred_GP_cov = self.models[j][k].predict(X_star_k,full_cov=self.full_cov)
#        LL = 0
#        sparse_mean[K_star_mask] = np.copy(pred_GP_mean)
#        if self.full_cov:
#            for idx,x_k in enumerate(np.where(self.Z_star[j]==k)[0]):
#                sparse_cov[x_k,K_star_mask] = np.copy(pred_GP_cov[idx,:]).reshape(1,-1)
#                sparse_cov[K_star_mask,x_k] = np.copy(pred_GP_cov[idx,:]).reshape(-1,1)
#        else:
#            sparse_cov[K_star_mask] = np.copy(pred_GP_cov)
#        return(sparse_mean, sparse_cov, LL)

    def prediction_combine(self):
        self.comm.barrier()
        if self.IS:
            self.proposal_prob[np.where(np.isnan(self.proposal_prob))] = -np.inf
            self.gather_weights = np.hstack(self.comm.allgather((self.proposal_prob)))
            self.proposal_prob = np.array_split(np.exp(self.gather_weights-logsumexp(self.gather_weights)), self.P)
            self.proposal_prob =self.comm.scatter(self.proposal_prob)
        else:
            self.proposal_prob[:] = 1./self.total_J
            self.gather_weights = np.copy(self.proposal_prob)

        self.gather_Y_predict = self.comm.gather(self.Y_star_predict)
        self.gather_Y_cov = self.comm.gather([self.Y_star_cov[j].toarray() for j in self.Y_star_cov.keys()])
        self.avg_Y_predict = self.comm.reduce(np.dot(self.proposal_prob, self.Y_star_predict))
        self.avg_cov = self.comm.reduce( np.sum([self.proposal_prob[j]*self.Y_star_cov[j].toarray() for j in self.Y_star_cov.keys()],axis=0))
        self.cluster_sizes = np.array(self.comm.gather([np.unique(self.Z_proposal[j]).size for j in xrange(self.J) ])).flatten()
        if self.rank ==0:
            end_time = time.time()- self.start_time
            if self.classification:
                self.avg_score = roc_auc_score(self.Y_star.flatten(), std_norm_cdf(self.avg_Y_predict.flatten()))
                pred_LL = np.clip(np.where(self.Y_star.flatten()==1,self.avg_Y_predict,1.-self.avg_Y_predict), 1e-9 ,np.inf)
                self.avg_LL = np.log(pred_LL).sum()
            else:
                self.avg_score = np.mean((self.avg_Y_predict.flatten() - self.Y_star.flatten())**2)
                if self.full_cov:
                    self.avg_cov += (1e-6)*np.eye(self.N_star)
                    self.avg_LL = stats.multivariate_normal.logpdf(self.Y_star.flatten(), self.avg_Y_predict.flatten(), self.avg_cov, allow_singular=True)
                else:
                    self.avg_LL = stats.norm.logpdf(self.Y_star.flatten(), self.avg_Y_predict.flatten(), np.sqrt(np.diag(self.avg_cov))).sum()
            print("%i %i %i %.2f %.2f %.2f %i %i %i " %(self.total_J, self.N_minibatch, self.K, self.avg_score, self.avg_LL, end_time, int(self.partition == "random"), int(self.IS), int(self.mb_weight == 1.)))

if __name__ == '__main__':
    from scipy.io import loadmat
    K_range= range(10,110,20)
    N= 1000
    J= 128
    X_full = np.memmap("gmm_X_mm_full",mode='r',dtype='float32',shape=(12000,100))
    Y_full = np.memmap("gmm_Y_mm_full",mode='r',dtype='float32',shape=(12000,1))
    print("ISMOE Regression")
    for K in K_range:
        kf=KFold(n_splits=5, random_state=0,shuffle=True)
        for train,test in kf.split(X_full,Y_full):
            X,Y = X_full[train], Y_full[train]
            X_star,Y_star = X_full[test], Y_full[test]
            igps = ISMOE(X = X, Y=Y, X_star= X_star, Y_star = Y_star,K=K, J=J,
                        IS=True,classification=False,N_minibatch = N,
                        partition="gmm", stationary=True, mb_upweight=True,
                        full_cov=False)
            igps.prediction_combine()
