import scipy.special
comb = scipy.special.comb
factorial = scipy.special.factorial
import torch
from torch.autograd import Function
import numpy as np




def _eye_like(M, device=None):
    """
    Creates an identity matrix of the same shape as M, if M is a (n,n)-matrix.
    If M is a batch of m matrices (i.e. a (m,n,n)-tensor), create a batch of
    (n,n)-identity-matrices.
    """
    assert(len(M.shape) in [2,3])
    assert(M.shape[-1]==M.shape[-2])
    n = M.shape[-1]
    if device is None:
        device = M.device
    eye = torch.eye(M.shape[-1], device=device) 
    if len(M.shape)==2:
        return eye
    else:
        m = M.shape[0]
        return eye.view(-1,n,n).expand(m, -1, -1)
    
def _eye(n, m=0, device=None):
    """
    Creates a (n,n)-identity-matrix or, if m>0, a batch of identity matrices.
    """
    eye = torch.eye(n, device=device)
    if m==0:
        return eye
    else:
        return eye.view(-1,n,n).expand(m, -1, -1)

def matrix_1_norm(A):
    norm, indices = torch.max(torch.sum(torch.abs(A),axis=-2),axis=-1)
    return norm

def _compute_scales(A):
    norm = matrix_1_norm(A)
    max_norm = torch.max(norm)
    s = torch.zeros_like(norm)
    
    if A.dtype == torch.float64:
        if A.requires_grad:
            ell = { 3: 0.010813385777848,
                    5: 0.199806320697895,
                    7: 0.783460847296204,
                    9: 1.782448623969279,
                   13: 4.740307543765127}
            if max_norm >= ell[9]:
                m = 13
                magic_number = ell[m]
                s = torch.relu_(torch.ceil(torch.log2_(norm / magic_number)))
            else:
                for m in [3,5,7,9]:
                    if max_norm < ell[m]:
                        magic_number = ell[m]
                        # results in s = 0
                        break
 
        else:
            # This is the magic number for exp(A) only, not for dexp(A)E
            ell = { 3: 0.014955852179582,
                    5: 0.253939833006323,
                    7: 0.950417899616293,
                    9: 2.097847961257068,
                   13: 5.371920351148152}
            if max_norm >= ell[9]:
                m = 13
                magic_number = ell[m]
                s = torch.relu_(torch.ceil(torch.log2_(norm / magic_number)))
            else:
                for m in [3,5,7,9]:
                    if max_norm < ell[m]:
                        magic_number = ell[m]
                        # results in s = 0
                        break
        
    elif A.dtype == torch.float32:
        # This is the magic number for exp(A) only, not for dexp(A)E
        if A.requires_grad:
            ell = {3: 0.308033041845330,
                   5: 1.482532614793145,
                   7: 3.248671755200478}
            if max_norm >= ell[5]:
                m = 7
                magic_number = ell[m]
                s = torch.relu_(torch.ceil(torch.log2_(norm / magic_number)))
            else:
                for m in [3,5]:
                    if max_norm < ell[m]:
                        # results in s = 0
                        magic_number = ell[m]
                        break
                        
        else:
            ell = {3: 0.425873001692283,
                   5: 1.880152677804762,
                   7: 3.925724783138660}
            if max_norm >= ell[5]:
                m = 7
                magic_number = ell[m]
                s = torch.relu_(torch.ceil(torch.log2_(norm / magic_number)))
            else:
                for m in [3,5]:
                    if max_norm < ell[m]:
                        # results in s = 0
                        magic_number = ell[m]
                        break
    return s, m

def _square(s, R, L=None):
    """
    Function which squares an m-batch of (n,n)-matrices s times,
    where s is a m-tensor denoting the number of squarings. 
    """
    s_max = torch.max(s).int()
    if s_max > 0:
        I = _eye_like(R)
        if L is not None: 
            O = torch.zeros_like(R)
        indices = [1 for k in range(len(R.shape)-1)]
    
    for i in range(s_max):
        # Multiply j-th matrix by dummy identity matrices if s<[j] < s_max,
        # to prevent squaring more often than desired.
        #temp = torch.clone(R)
        mask = (i >= s)
        matrices_mask = mask.view(-1,*indices)
        
        # L <- R@L+L@R.
        # If replace matrices by identity matrices in the first
        # multiplication and by zero-matrices in the second multiplication,
        # which results in L <- L
        temp_eye = torch.clone(R).masked_scatter(matrices_mask, I)
        if L is not None:
            temp_zeros = torch.clone(R).masked_scatter(matrices_mask, O)
            L = temp_eye @ L + temp_zeros @ L 
        R = R @ temp_eye
        del temp_eye, mask
        
    if L is not None:
        return R, L
    else:
        return R

def _expm_scaling_squaring(A):
    """
    Matrix exponentiation by the 'scaling-and-squaring' algorithm. 
    This is based on the observation that exp(A) = exp(A/k)^k, where e.g. k=2^s. 
    The exponential exp(A/(2^s)) is calculated by a diagonal Padé approximation, where s 
    is chosen based on the 1-norm of A, such that certain approximation guarantees can be given.
    exp(A) is then calculated by repeated squaring via exp(A/(2^s))^(2^s).
    This function works both for (n,n)-tensors as well as batchwise for (m,n,n)-tensors.
    """
    
    # Is A just a n-by-n matrix or does it have an extra batch dimension?
    assert(A.shape[-1]==A.shape[-2] and len(A.shape) in [2,3])
    has_batch_dim = True if len(A.shape)==3 else False
    
        
    # Step 1: Scale matrices in A according to a norm criterion
    s, m = _compute_scales(A)
    if torch.max(s) > 0:
        indices = [1 for k in range(len(A.shape)-1)]
        A = A * torch.pow(2,-s).view(-1,*indices)
    
    # Step 2: Calculate the exponential of the scaled matrices via diagonal
    # Padé approximation.
    exp_A = _expm_pade(A, m)
    
    # Step 3: Square the matrices an appropriate number of times to revert
    # the scaling in step 1.
    exp_A = _square(s, exp_A)

    return exp_A

def _expm_frechet_scaling_squaring(A, E, adjoint=False):
    """
    Fréchet derivative of matrix exponentiation by the 'scaling-and-squaring' algorithm.
    """
    
    # Is A just a n-by-n matrix or does it have an extra batch dimension?
    assert(A.shape[-1]==A.shape[-2] and len(A.shape) in [2,3])
    has_batch_dim = True if len(A.shape)==3 else False
    

    if adjoint == True:
        A = torch.transpose(A,-1,-2)
    
        
    # Step 1: Scale matrices in A and E according to a norm criterion
    s, m = _compute_scales(A)
    if torch.max(s) > 0:
        indices = [1 for k in range(len(A.shape)-1)]
        scaling_factors = torch.pow(2,-s).view(-1,*indices)
        A = A * scaling_factors
        E = E * scaling_factors
    
    # Step 2: 
    exp_A, dexp_A = _expm_frechet_pade(A, E, m)
    exp_A, dexp_A = _square(s, exp_A, dexp_A)

    return dexp_A


def _expm_pade(A, m=7):
    assert(m in [3,5,7,9,13])
    
    # The following are values generated as 
    # b = torch.tensor([_fraction(m, k) for k in range(m+1)]),
    # but reduced to natural numbers such that b[-1]=1. This still works,
    # because the same constants are used in the numerator and denominator
    # of the Padé approximation.
    if m == 3:
        b = [120., 60., 12., 1.]
    elif m == 5:
        b = [30240., 15120., 3360., 420., 30., 1.]
    elif m == 7:
        b = [17297280., 8648640., 1995840., 277200., 25200., 1512., 56., 1.]
    elif m == 9:
        b = [17643225600., 8821612800., 2075673600., 302702400., 30270240., 
             2162160., 110880., 3960., 90., 1.]
    elif m == 13:
        b = [64764752532480000., 32382376266240000., 7771770303897600., 1187353796428800., 
             129060195264000., 10559470521600., 670442572800., 33522128640., 1323241920., 
             40840800., 960960., 16380., 182., 1.]
    
    
    # pre-computing terms
    I = _eye_like(A)
    U = b[1]*I
    V = b[0]*I
    if m!=13: # There is a more efficient algorithm for m=13
        if m >= 3:
            A_2 = A @ A
            U = U + b[3]*A_2
            V = V + b[2]*A_2
        if m >= 5:
            A_4 = A_2 @ A_2
            U = U + b[5]*A_4
            V = V + b[4]*A_4
        if m >= 7:
            A_6 = A_4 @ A_2
            U = U + b[7]*A_6
            V = V + b[6]*A_6
        if m == 9: 
            A_8 = A_4 @ A_4
            U = U + b[9]*A_8
            V = V + b[8]*A_8
    else:
        raise NotImplementedError("m=13 not implemented")
    U = A @ U
    
    del A_2
    if m>=5: del A_4
    if m>=7: del A_6
    if m==9: del A_8
    
    R = torch.lu_solve(U+V, *torch.lu(-U+V))

    del U, V
    return R

def _expm_frechet_pade(A, E, m=7, adjoint=False):
        
    assert(m in [3,5,7,9,13])
    
    if m == 3:
        b = [120., 60., 12., 1.]
    elif m == 5:
        b = [30240., 15120., 3360., 420., 30., 1.]
    elif m == 7:
        b = [17297280., 8648640., 1995840., 277200., 25200., 1512., 56., 1.]
    elif m == 9:
        b = [17643225600., 8821612800., 2075673600., 302702400., 30270240., 
             2162160., 110880., 3960., 90., 1.]
    elif m == 13:
        b = [64764752532480000., 32382376266240000., 7771770303897600., 1187353796428800., 
             129060195264000., 10559470521600., 670442572800., 33522128640., 1323241920., 
             40840800., 960960., 16380., 182., 1.]

    # Efficiently compute series terms of M_i (and A_i if needed).
    # Not very pretty, but more readable than the alternatives.
    if m!=13:
        if m >= 3:
            M_2 = A @ E + E @ A
            A_2 = A @ A 
            V = b[2]*A_2
            U = b[3]*A_2
            L_U = b[3]*M_2
            L_V = b[2]*M_2
        if m >= 5:
            M_4 = A_2 @ M_2 + M_2 @ A_2
            A_4 = A_2 @ A_2
            V = V + b[4]*A_4
            U = U + b[5]*A_4
            L_U = L_U + b[5]*M_4
            L_V = L_V + b[4]*M_4
        if m >= 7:
            M_6 = A_4 @ M_2 + M_4 @ A_2
            A_6 = A_4 @ A_2
            V = V + b[6]*A_6
            U = U + b[7]*A_6
            L_U = L_U + b[7]*M_6
            L_V = L_V + b[6]*M_6
        if m == 9:
            M_8 = A_4 @ M_4 + M_4 @ A_4
            A_8 = A_4 @ A_4
            V = V + b[8]*A_8
            U = U + b[9]*A_8
            L_U = L_U + b[8]*M_8
            L_V = L_V + b[6]*M_6
    else:
        raise NotImplementedError("m = 13 not implemented")

    I = _eye_like(A)
    U = U + b[1]*I
    V = U + b[0]*I
    del I
        
    L_U = A @ L_U
    L_U = L_U + E @ U
    
    U = A @ U
    
    lu_decom = torch.lu(-U + V)
    exp_A = torch.lu_solve(U + V, *lu_decom)
    dexp_A = torch.lu_solve(L_U + L_V + (L_U - L_V) @ exp_A, *lu_decom)
     
    return exp_A, dexp_A
    

class expm(Function):
    @staticmethod
    def forward(ctx, M):
        expm_M = _expm_scaling_squaring(M)
        ctx.save_for_backward(M)
        return expm_M

    @staticmethod
    def backward(ctx, grad_out):
        M = ctx.saved_tensors[0]
        dexpm = _expm_frechet_scaling_squaring(
            M, grad_out, adjoint=True)
        return dexpm    
    
    
    
    
    
    
"""
Just for comparison: a minimal, inefficient implementation.
"""    
    
def _fraction(m,k):
    return comb(m,k) * factorial(2*m-k) / factorial(2*m) 

def _pade_poly(z, n):
    size = z.shape[-1]
    power = torch.eye(size, device=z.device).reshape(1,size,size)
    if len(z.shape) > 2:
        power = power.repeat(z.shape[0], 1, 1)
    result = power
    for k in range(1,n):
        power = z @ power
        result = result + _fraction(n,k) * power 
    return result

def _exp_pade_generic(A, m=7):
    """
    Minimal, inefficient implementation of the [m/m]-Padé approximation of the
    matrix exponential.
    """
    LU = torch.lu(_pade_poly(-A,m))
    result = torch.lu_solve(_pade_poly(A,m),*LU)
    return result