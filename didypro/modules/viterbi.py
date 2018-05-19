import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence

from didypro.modules.local import operators


def _topological_loop(theta, batch_sizes, operator='softmax', adjoint=False,
                      Q=None, Qt=None):
    operator = operators[operator]
    new = theta.new
    B = batch_sizes[0].item()
    T = len(batch_sizes)
    L, S, _ = theta.size()
    if adjoint:
        Qd = new(L + B, S, S).zero_()
        Qtd = new(B, S).zero_()
        Vd = new(L + B, S).zero_()
        Vdt = new(B).zero_()
    else:
        Q = new(L + B, S, S).zero_()
        Qt = new(B, S).zero_()
        V = new(L + B, S).zero_()
        Vt = new(B).zero_()

    left = B
    term_right = B
    prev_length = B

    for t in range(T + 1):
        if t == T:
            cur_length = 0
        else:
            cur_length = batch_sizes[t]
        right = left + cur_length
        prev_left = left - prev_length
        prev_cut = right - prev_length
        len_term = prev_length - cur_length
        if cur_length != 0:
            # -B account for padding
            if adjoint:
                M = (theta[left - B:right - B]
                     + Vd[prev_left:prev_cut][:, None, :])
                Vd[left:right] = torch.sum(Q[left:right] * M, dim=2)
                Qd[left:right] = operator.hessian_product(Q[left:right], M)
            else:
                M = (theta[left - B:right - B]
                     + V[prev_left:prev_cut][:, None, :])
                V[left:right], Q[left:right] = operator.max(M)
        term_left = term_right - len_term
        if len_term != 0:
            if adjoint:
                M = Vd[prev_cut:left]
                Vdt[term_left:term_right] = torch.sum(
                    Qt[term_left:term_right] * M)
                Qtd[term_left:term_right] = operator.hessian_product(
                    Qt[term_left:term_right][:, None, :], M[:, None, :])[:, 0]
            else:
                M = V[prev_cut:left]
                Vt[term_left:term_right], Qt[term_left:term_right] \
                    = operator.max(M[:, None, :])
        term_right = term_left
        left = right
        prev_length = cur_length
    if adjoint:
        return Vdt, Qd, Qtd
    else:
        return Vt, Q, Qt


def _reverse_loop(Q, Qt, Ut, batch_sizes, adjoint=False,
                  U=None, Qd=None, Qdt=None):
    new = Q.new

    B = batch_sizes[0].item()
    T = len(batch_sizes)
    L, S, _ = Q.size()
    L = L - B

    if adjoint:
        Ed = new(L + B, S, S).zero_()
        Edoff = new(L, S, S).zero_()
        Ud = new(L + B, S).zero_()
        Udt = new(B).zero_()
    else:
        E = new(L + B, S, S).zero_()
        Eoff = new(L, S, S).zero_()
        U = new(L + B, S).zero_()
        # Ut = Ut

    right = L + B
    term_left = 0
    prev_length = 0
    off_right = L

    for t in reversed(range(-1, T)):
        if t == -1:
            cur_length = B
        else:
            cur_length = batch_sizes[t]
        left = right - cur_length
        off_left = off_right - prev_length
        cut = left + prev_length
        len_term = cur_length - prev_length
        if prev_length != 0:
            prev_left, prev_cut = right, right + prev_length
            if adjoint:
                Ed[left:cut] = (Q[prev_left:prev_cut] *
                                Ud[prev_left:prev_cut][:, :, None] +
                                Qd[prev_left:prev_cut] *
                                U[prev_left:prev_cut][:, :, None])
                Ud[left:cut] = torch.sum(Ed[left:cut], dim=1)
                # TODO remove
                Edoff[off_left:off_right] = Ed[left:cut]
            else:
                E[left:cut] = (Q[prev_left:prev_cut] *
                               U[prev_left:prev_cut][:, :, None])
                U[left:cut] = torch.sum(E[left:cut], dim=1)
                Eoff[off_left:off_right] = E[left:cut]
        term_right = term_left + len_term
        if len_term > 0:
            if adjoint:
                Ud[cut:right] = (Qt[term_left:term_right] *
                                 Udt[term_left:term_right][:, None] +
                                 Qdt[term_left:term_right] *
                                 Ut[term_left:term_right][:, None])

            else:
                U[cut:right] = (Qt[term_left:term_right]
                                * Ut[term_left:term_right][:, None])
        term_left = term_right
        right = left
        off_right = off_left
        prev_length = cur_length
    if not adjoint:
        return Eoff, U, Ut
    else:
        return Edoff, Ud, Udt


class ViterbiFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, theta, batch_sizes, operator):
        Vt, Q, Qt = _topological_loop(theta, batch_sizes,
                                      operator=operator, adjoint=False)
        ctx.save_for_backward(theta, Q, Qt)
        ctx.others = batch_sizes, operator
        return Vt

    @staticmethod
    def backward(ctx, M):
        theta, Q, Qt = ctx.saved_tensors
        batch_sizes, operator = ctx.others
        return ViterbiFunctionBackward.apply(theta,
            M, Q, Qt, batch_sizes, operator), None, None


class ViterbiFunctionBackward(torch.autograd.Function):

    @staticmethod
    def forward(ctx, theta, M, Q, Qt, batch_sizes, operator):
        Eoff, U, Ut = _reverse_loop(Q, Qt, M, batch_sizes,
                                    adjoint=False)
        ctx.save_for_backward(Q, Qt, U, Ut)
        ctx.others = batch_sizes, operator
        return Eoff

    @staticmethod
    def backward(ctx, Z):
        Q, Qt, U, Ut = ctx.saved_tensors
        batch_sizes, operator = ctx.others
        Vdt, Qd, Qdt = _topological_loop(Z, batch_sizes,
                                         operator=operator,
                                         adjoint=True,
                                         Q=Q, Qt=Qt)
        Edoff, Ud, Udt = _reverse_loop(Q, Qt, Ut, batch_sizes,
                                       adjoint=True,
                                       Qd=Qd, Qdt=Qdt,
                                       U=U)
        return Edoff, Vdt, None, None, None, None


class PackedViterbi(nn.Module):
    def __init__(self, operator):
        super().__init__()
        self.operator = operator

    def forward(self, theta, batch_sizes):
        return ViterbiFunction.apply(theta, batch_sizes, self.operator)


class Viterbi(nn.Module):
    def __init__(self, operator):
        super().__init__()
        self.operator = operator

    def forward(self, theta, lengths=None):
        if lengths is None:
            lengths = torch.LongTensor(theta.shape[1],
                                       device=theta.device).fill_(theta.shape[0])
        theta, batch_sizes = pack_padded_sequence(theta, lengths)
        return ViterbiFunction.apply(theta, batch_sizes, self.operator)