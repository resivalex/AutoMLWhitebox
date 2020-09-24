from typing import Sequence, Tuple, Any, Dict, List, Union, Optional
from sklearn.svm import l1_min_c
from sklearn.metrics import roc_auc_score
from sklearn.linear_model import LogisticRegression
import numpy as np
from scipy import stats
from copy import deepcopy


def refit_reg(X: np.ndarray, y: np.ndarray, l1_base_step: int, l1_exp_step: float, 
                    max_penalty: float, interp: bool = True
                    ) -> Tuple[np.ndarray, float, np.ndarray]:
    
    clf = LogisticRegression(penalty='l1', solver='saga', warm_start=True, 
                             intercept_scaling=100000)
    cs = l1_min_c(X, y, loss='log', fit_intercept=True) * np.logspace(0, l1_exp_step, l1_base_step)
    cs = cs[cs <= max_penalty]
    # add final penalty
    if cs[-1] < max_penalty:
        cs = list(cs)
        cs.append(max_penalty)
    
    
    # fit path 
    weights, intercepts = [], []
    for c in cs:
        clf.set_params(C=c)
        clf.fit(X, y)
        weights.append(deepcopy(clf.coef_[0]))
        intercepts.append(clf.intercept_[0]) 
        
    if not interp:
        w, i = weights[-1], intercepts[-1]
        neg = w != 0
        return w[neg], i, neg
        
    for w, i in zip(weights[::-1], intercepts[::-1]):
        
        pos = (w > 0).sum()
        if pos > 0:
            continue
            
        neg = w < 0
        return w[neg], i, neg
    # заглушка, если уж херня какая-то получилась - верни что есть 
    # return w[neg], i, neg
    raise ValueError('No negative weights grid')

    
    
def refit_simple(X: np.ndarray, y: np.ndarray, interp: bool = True, 
                 p_val: float = 0.05, X_val: Optional[np.ndarray] = None, y_val: Optional[np.ndarray] = None
                ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    
    sl_ok = np.ones(X.shape[1], dtype=bool)
    
    n = -1
    
    while True:
        n += 1
        assert sl_ok.sum() > 0, 'No features left to fit on iter'.format(n)
        
        print('Iter {0} of final refit starts with {1} features'.format(n, sl_ok.sum()))
        
        X_ = X[:, sl_ok]
        # индексы в исходном массиве
        ok_idx = np.arange(X.shape[1])[sl_ok]
        
        clf = LogisticRegression(penalty='none', solver='lbfgs', warm_start=False, 
                             intercept_scaling=1)
        clf.fit(X_, y)
        
        # check negative coefs here if interp
        sl_pos_coef = np.zeros((X_.shape[1], ), dtype=np.bool)
        if interp:
            sl_pos_coef = clf.coef_[0] >= 0
        
        # если хотя бы один неотрицательный - убирай самый большой и по новой
        if sl_pos_coef.sum() > 0:
            max_coef_idx = clf.coef_[0].argmax()
            sl_ok[ok_idx[max_coef_idx]] = False
            continue
            
        # если прошли все отрицательные смотрим на pvalue
        p_vals, b_var = calc_p_val(X_, clf.coef_[0], clf.intercept_[0])
        # без интерсепта
        p_vals_f = p_vals[:-1]
        
        model_p_vals = p_vals.copy()
        model_b_var = b_var.copy
        
        # если хотя бы один больше p_val - дропай самый большой и погнали по новой
        if p_vals_f.max() > p_val:
            max_p_val_idx = p_vals_f.argmax()
            sl_ok[ok_idx[max_p_val_idx]] = False
            continue
            
        if X_val is not None:
            # то же самое на валидационной выборке
            print('Validation data checks')
            X_val_ = X_val[:, sl_ok]
            
            p_vals, b_var = calc_p_val_on_valid(X_val_, y_val)
            p_vals_f = p_vals[:-1]
            
            # если хотя бы один больше p_val - дропай самый большой и погнали по новой
            if p_vals_f.max() > p_val:
                max_p_val_idx = p_vals_f.argmax()
                sl_ok[ok_idx[max_p_val_idx]] = False
                continue
            
        return clf.coef_[0], clf.intercept_[0], sl_ok, model_p_vals, model_b_var
            
        
        
def calc_p_val(X: np.ndarray, weights: np.ndarray, intercept: float) -> Tuple[np.ndarray, np.ndarray]:
    
    coef_ = np.concatenate([weights, [intercept]])
    X = np.concatenate([X, np.ones((X.shape[0], 1))], axis=1)
    P_ = 1 / (1 + np.exp(-np.dot(X, coef_)))
    P_ = P_ * (1 - P_)
    H = np.dot((P_[:, np.newaxis] * X).T, X)
    
    Hinv = np.linalg.inv(H)
    b_var = Hinv.diagonal()
    Wstat = (coef_ ** 2) / b_var

    p_vals = 1 - stats.chi2(1).cdf(Wstat)
    
    return p_vals, b_var


def calc_p_val_on_valid(X, y) -> Tuple[np.ndarray, np.ndarray]:
    
    pv_mod = LogisticRegression(penalty='none', solver='lbfgs')
    pv_mod.fit(X, y)
    
    return calc_p_val(X, pv_mod.coef_[0], pv_mod.intercept_[0])
    
    
    
    
    
