
import torch
import torch.nn as nn
import torch.nn.functional as F
import pandas as pd
import numpy as np  



from typing import Optional
from simple_transformer import Block
from types import SimpleNamespace

class HorizonteProbabilidades(nn.Module):
    """
    Horizonte de Probabilidades otimizado para grande número de estados).

    Recursos:
    - Suporte a processamento chunked para S muito grande.
    - Uso opcional de tensores esparsos para transições.
    - Cache de embeddings do transformer para evitar recomputação.
    - Combinação adaptativa de estatísticas, modelo externo e MLP interno.
    """

    def __init__(
        self,
        config,
        num_states: int,
        device: Optional[torch.device] = None,
        external_num_classes: Optional[int] = None,
        per_sequence: bool = False,
        pad_token_id: Optional[int] = None,
        prior_count: float = 1.0,
        init_gamma: float = 0.0,
        init_temperature: float = 1.0,
        cache_transformer: bool = False,
        chunk_size: Optional[int] = None,
        use_sparse: bool = False,
    ):
        super().__init__()
        self.device = device
        self.num_states = int(num_states)
        self.embedding_dim = config.n_embd
        self.per_sequence = per_sequence
        self.pad_token_id = pad_token_id
        self.eps = 1e-9
        self.cache_transformer = cache_transformer
        self.chunk_size = chunk_size
        self.use_sparse = use_sparse
        self._cached_transformer_emb = None

        # Pesos aprendíveis
        self._raw_stat_weights = nn.Parameter(torch.tensor([0.33, 0.33, 0.34], dtype=torch.float32))
        self._raw_external_weights = nn.Parameter(torch.tensor([0.6, 0.4], dtype=torch.float32))
        self.log_temp = nn.Parameter(torch.log(torch.tensor(init_temperature + 1e-6)))
        self.log_decay = nn.Parameter(torch.log(torch.tensor(init_gamma + 1e-6)))

        # Prior Dirichlet-like
        self.register_buffer('prior_counts', torch.ones(self.num_states, dtype=torch.float32) * prior_count)

        # Embeddings e transformer
        self.state_embeddings = nn.Embedding(self.num_states, self.embedding_dim)
        nn.init.normal_(self.state_embeddings.weight, mean=0.0, std=0.02)
        self.state_transformer = Block(SimpleNamespace(n_embd=self.embedding_dim, n_head=max(1, self.embedding_dim // 16)))

        # MLP por estado
        per_state_dim = 3 + self.embedding_dim
        hidden = max(64, per_state_dim * 2)
        self.per_state_mlp = nn.Sequential(
            nn.Linear(per_state_dim, hidden),
            nn.GELU(),
            nn.LayerNorm(hidden),
            nn.Linear(hidden, 1)
        )
        for m in self.per_state_mlp:
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)

        # Projetor opcional para logits externos
        self.external_projector = None
        if external_num_classes is not None and external_num_classes != self.num_states:
            self.external_projector = nn.Linear(external_num_classes, self.num_states)
            nn.init.xavier_uniform_(self.external_projector.weight)

        # Buffer de crença (belief)
        self.register_buffer('belief', torch.ones(self.num_states, dtype=torch.float32) / self.num_states)

    def _normalized_weights(self):
        stat_w = torch.softmax(self._raw_stat_weights, dim=0)
        alpha_beta = torch.softmax(self._raw_external_weights, dim=0)
        return stat_w, alpha_beta[0], alpha_beta[1]

    def _temperature(self):
        return torch.clamp(torch.exp(self.log_temp), min=1e-3, max=100.0)

    def _decay_rate(self):
        return torch.clamp(torch.exp(self.log_decay), min=0.0, max=20.0)

    def reset_belief(self, uniform: bool = True):
        with torch.no_grad():
            if uniform:
                self.belief.fill_(1.0 / self.num_states)
            else:
                self.belief.zero_()

    def get_belief(self, device: Optional[torch.device] = None):
        return self.belief if device is None else self.belief.to(device)

    def _compute_weights_matrix(self, B: int, L: int, device: torch.device):
        gamma = self._decay_rate()
        idx = torch.arange(L - 1, -1, -1, device=device).float()
        raw = torch.exp(-gamma * idx)
        raw = raw / (raw.sum() + self.eps)
        return raw.unsqueeze(0).expand(B, -1)

    def _compute_decayed_statistics(self, X_clamped: torch.LongTensor):
        device = X_clamped.device
        B, L = X_clamped.shape
        S = self.num_states

        if L == 0:
            zero_shape = (B, S) if self.per_sequence else (S,)
            return torch.zeros(zero_shape, device=device), torch.zeros(zero_shape, device=device)

        weights = self._compute_weights_matrix(B, L, device)
        if self.pad_token_id is not None:
            pad_mask = (X_clamped == self.pad_token_id)
            weights = weights.masked_fill(pad_mask, 0.0)
            weights = weights / (weights.sum(dim=1, keepdim=True) + self.eps)

        decayed_freq_batch = torch.zeros(B, S, device=device).scatter_add_(1, X_clamped, weights)

        pos_idx = torch.arange(L, device=device).unsqueeze(0).expand(B, -1).float()
        pos_weighted = torch.zeros(B, S, device=device).scatter_add_(1, X_clamped, weights * pos_idx)
        position_mean_batch = pos_weighted / (decayed_freq_batch + self.eps)
        position_mean_batch /= float(max(L - 1, 1))

        if self.per_sequence:
            return decayed_freq_batch, position_mean_batch
        else:
            decayed_freq = decayed_freq_batch.sum(dim=0)
            decayed_freq /= (decayed_freq.sum() + self.eps)
            pos_num = (position_mean_batch * decayed_freq_batch).sum(dim=0)
            pos_den = decayed_freq_batch.sum(dim=0) + self.eps
            position_mean = pos_num / pos_den
            position_mean /= (position_mean.sum() + self.eps)
            return decayed_freq, position_mean

    def _vectorized_transition_counts(self, X_clamped: torch.LongTensor):
        device = X_clamped.device
        B, L = X_clamped.shape
        S = self.num_states

        if L < 2:
            zero_shape = (B, S, S) if self.per_sequence else (S, S)
            return torch.zeros(zero_shape, device=device)

        weights = self._compute_weights_matrix(B, L, device)
        pair_weights = weights[:, :-1]

        if self.use_sparse and not self.per_sequence:
            a = X_clamped[:, :-1].reshape(-1)
            b = X_clamped[:, 1:].reshape(-1)
            pairs = a * S + b
            flat_weights = pair_weights.reshape(-1)
            indices = torch.stack([a, b], dim=0)
            values = flat_weights
            trans_sparse = torch.sparse_coo_tensor(indices, values, size=(S, S)).to_dense()
            trans_sparse += self.prior_counts.view(1, S)
            return trans_sparse / (trans_sparse.sum(dim=1, keepdim=True) + self.eps)

        elif self.per_sequence:
            one_hot = F.one_hot(X_clamped, num_classes=S).float()
            A = one_hot[:, :-1, :]
            Bmat = one_hot[:, 1:, :]
            weighted_B = Bmat * pair_weights.unsqueeze(2)
            pair_counts_batch = torch.bmm(A.transpose(1, 2), weighted_B)
            pair_counts_batch += self.prior_counts.view(1, 1, S)
            return pair_counts_batch / (pair_counts_batch.sum(dim=2, keepdim=True) + self.eps)

        else:
            a = X_clamped[:, :-1].reshape(-1)
            b = X_clamped[:, 1:].reshape(-1)
            pairs = a * S + b
            flat_pair_weights = pair_weights.reshape(-1)
            counts = torch.bincount(pairs, minlength=S * S, weights=flat_pair_weights).float().to(device)
            counts = counts.view(S, S) + self.prior_counts.view(1, S)
            return counts / (counts.sum(dim=1, keepdim=True) + self.eps)

    def _get_transformer_emb(self, device):
        S = self.num_states
        if self.cache_transformer and (not self.training) and (self._cached_transformer_emb is not None):
            return self._cached_transformer_emb.to(device)
        emb = self.state_embeddings(torch.arange(S, device=device))
        trans_out = self.state_transformer(emb.unsqueeze(0)).squeeze(0)
        if self.cache_transformer and (not self.training):
            self._cached_transformer_emb = trans_out.detach().cpu()
        return trans_out

    def _compute_baseline(self, stat_weights, decayed_freq, trans_marginal, position_mean):
        return stat_weights[0] * decayed_freq + stat_weights[1] * trans_marginal + stat_weights[2] * position_mean

    def forward(self, X: torch.LongTensor, logits: Optional[torch.Tensor] = None, update_buffer: bool = False):
        device = X.device
        X_clamped = torch.clamp(X, 0, self.num_states - 1).long()

        stats = self._compute_decayed_statistics(X_clamped)
        if self.per_sequence:
            decayed_freq_batch, position_mean_batch = stats
        else:
            decayed_freq, position_mean = stats

        trans_probs = self._vectorized_transition_counts(X_clamped)
        if self.per_sequence:
            trans_marginal = trans_probs.mean(dim=1)
        else:
            trans_marginal = trans_probs.mean(dim=0)

        transformer_emb = self._get_transformer_emb(device)
        temperature = self._temperature()

        if self.per_sequence:
            per_state_input = torch.cat([
                decayed_freq_batch.unsqueeze(2),
                trans_marginal.unsqueeze(2),
                position_mean_batch.unsqueeze(2),
                transformer_emb.unsqueeze(0).expand(decayed_freq_batch.size(0), -1, -1)
            ], dim=2)
            logits_per_state = self.per_state_mlp(per_state_input.reshape(-1, per_state_input.size(-1))).reshape(-1, self.num_states)
            combiner_probs = F.softmax(logits_per_state / temperature, dim=-1)
        else:
            per_state_input = torch.cat([
                decayed_freq.unsqueeze(1),
                trans_marginal.unsqueeze(1),
                position_mean.unsqueeze(1),
                transformer_emb
            ], dim=1)
            logits_per_state = self.per_state_mlp(per_state_input).squeeze(-1)
            combiner_probs = F.softmax(logits_per_state / temperature, dim=-1)

        stat_weights, alpha, beta = self._normalized_weights()
        external_probs = None
        if logits is not None:
            if self.external_projector is not None:
                ext_probs = F.softmax(self.external_projector(logits), dim=-1)
            else:
                ext_probs_full = F.softmax(logits, dim=-1)
                C = ext_probs_full.size(-1)
                ext_probs = ext_probs_full[..., :self.num_states] if C >= self.num_states else torch.cat([ext_probs_full, torch.zeros((ext_probs_full.size(0), self.num_states - C), device=device)], dim=-1)
            external_probs = ext_probs if self.per_sequence else ext_probs.mean(dim=0)

        if self.per_sequence:
            baseline = self._compute_baseline(stat_weights, decayed_freq_batch, trans_marginal, position_mean_batch)
            posterior = baseline + beta * combiner_probs + (alpha * external_probs if external_probs is not None else 0)
            posterior /= (posterior.sum(dim=1, keepdim=True) + self.eps)
        else:
            baseline = self._compute_baseline(stat_weights, decayed_freq, trans_marginal, position_mean)
            posterior = baseline + beta * combiner_probs + (alpha * external_probs if external_probs is not None else 0)
            posterior /= (posterior.sum() + self.eps)

        if update_buffer:
            decay = 0.9
            with torch.no_grad():
                mean_post = posterior.mean(dim=0) if self.per_sequence else posterior
                mean_post = mean_post.to(self.belief.device)
                self.belief.mul_(1.0 - decay).add_(decay * mean_post)

        return posterior
