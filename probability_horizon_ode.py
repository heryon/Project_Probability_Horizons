import torch
import torch.nn as nn
import torch.nn.functional as F

# Causal Self-Attention

class CausalSelfAttention(nn.Module):
    """Multi-head causal self-attention (GPT-style)."""

    def __init__(self, config):
        super().__init__()
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        self.head_dim = config.n_embd // config.n_head

        self.c_attn = nn.Linear(config.n_embd, 3 * config.n_embd)
        self.c_proj = nn.Linear(config.n_embd, config.n_embd)

        nn.init.xavier_uniform_(self.c_attn.weight)
        nn.init.constant_(self.c_proj.weight, 1)

    def forward(self, x):
        """Compute causal self-attention over the input sequence."""
        B, T, C = x.size()
        qkv = self.c_attn(x).chunk(3, dim=-1)
        q, k, v = [t.view(B, T, self.n_head, self.head_dim).transpose(1, 2) for t in qkv]
        y = F.scaled_dot_product_attention(q, k, v, is_causal=True)
        y = y.transpose(1, 2).contiguous().view(B, T, C)
        return self.c_proj(y)


# Feed-Forward Network

class MLP(nn.Module):
    """Transformer feed-forward network."""

    def __init__(self, config):
        super().__init__()
        self.c_fc = nn.Linear(config.n_embd, 4 * config.n_embd)
        self.c_proj = nn.Linear(4 * config.n_embd, config.n_embd)
        self.act = nn.GELU()

        nn.init.xavier_uniform_(self.c_fc.weight)
        nn.init.xavier_uniform_(self.c_proj.weight)

    def forward(self, x):
        """Apply non-linear transformation."""
        return self.c_proj(self.act(self.c_fc(x)))


# Transformer Block

class Block(nn.Module):
    """Pre-norm Transformer block with residual connections."""

    def __init__(self, config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)
        self.attn = CausalSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)

    def forward(self, x):
        """Apply attention and MLP with residual connections."""
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x


#Probability Horizon with ODE Dynamics

class HorizonteProbabilidadesODE(nn.Module):
    """
    Dynamical system evolving on the probability simplex.

    Discrete dynamics:
        P_{t+1} = N[ λ1 F + λ2 T + λ3 S + α C + β M ]

    Continuous dynamics:
        dP/dt = Φ(P)
    """

    def __init__(self, config, num_pedras: int):
        super().__init__()

        # Dimensionality and hyperparameters
        self.num_pedras = num_pedras
        self.embedding_dim = config.n_embd

        self.lambda1 = config.lambda1
        self.lambda2 = config.lambda2
        self.lambda3 = config.lambda3
        self.alpha = config.alpha
        self.beta = config.beta

        self.temperature = getattr(config, "temperature", 1.0)
        self.epsilon = 1e-8

        # Structural memory (M)
        self.embeddings = nn.Embedding(num_pedras, self.embedding_dim)
        nn.init.normal_(self.embeddings.weight, mean=0.0, std=0.02)

        self.transformer_block = Block(config)
        self.prob_projection = nn.Linear(self.embedding_dim, num_pedras)
        nn.init.xavier_uniform_(self.prob_projection.weight)

        # Internal probability state P(t)
        self.register_buffer("P", torch.full((num_pedras,), 1.0 / num_pedras))

    def normalize(self, P: torch.Tensor) -> torch.Tensor:
        """Project a vector onto the probability simplex."""
        P = torch.clamp(P, min=0.0)
        return P / (P.sum() + self.epsilon)

    def F_frequencia(self, X: torch.Tensor) -> torch.Tensor:
        """Empirical frequency operator."""
        X = torch.clamp(X, 1, self.num_pedras) - 1
        freq = torch.bincount(X.flatten(), minlength=self.num_pedras).float()
        return self.normalize(freq)

    def T_transicao(self, X: torch.Tensor) -> torch.Tensor:
        """Mean state transition operator."""
        X = torch.clamp(X, 1, self.num_pedras) - 1
        T = torch.zeros(self.num_pedras, self.num_pedras, device=X.device)
        for seq in X:
            i, j = seq[:-1], seq[1:]
            T.index_put_((i, j), torch.ones_like(i, dtype=torch.float), accumulate=True)
        T = T / (T.sum(dim=1, keepdim=True) + self.epsilon)
        return self.normalize(T.mean(dim=0))

    def S_posicional(self, X: torch.Tensor) -> torch.Tensor:
        """Average positional distribution operator."""
        X = torch.clamp(X, 1, self.num_pedras) - 1
        pos = torch.zeros(self.num_pedras, device=X.device)
        for t in range(X.size(1)):
            pos += torch.bincount(X[:, t], minlength=self.num_pedras).float()
        return self.normalize(pos)

    def C_classificacao(self, logits: torch.Tensor) -> torch.Tensor:
        """External supervised classification signal."""
        probs = F.softmax(logits, dim=-1).mean(dim=(0, 1))
        return self.normalize(probs)

    def M_memoria(self) -> torch.Tensor:
        """Internal memory induced by the Transformer."""
        idx = torch.arange(self.num_pedras, device=self.P.device)
        emb = self.embeddings(idx)
        h = self.transformer_block(emb.unsqueeze(0)).squeeze(0)
        logits = self.prob_projection(h) / self.temperature
        return self.normalize(F.softmax(logits, dim=-1).mean(dim=0))

    def Phi(self, X: torch.Tensor, logits: torch.Tensor | None = None) -> torch.Tensor:
        """Vector field defining the probability flow."""
        Fp = self.F_frequencia(X)
        Tp = self.T_transicao(X)
        Sp = self.S_posicional(X)
        Mp = self.M_memoria()
        Cp = self.C_classificacao(logits) if logits is not None else torch.zeros_like(self.P)

        P_target = (
            self.lambda1 * Fp +
            self.lambda2 * Tp +
            self.lambda3 * Sp +
            self.alpha * Cp +
            self.beta * Mp
        )
        return self.normalize(P_target) - self.P

    def forward(self, X: torch.Tensor, logits: torch.Tensor | None = None, dt: float = 1.0) -> torch.Tensor:
        """Advance the probability state using explicit Euler integration."""
        self.P = self.normalize(self.P + dt * self.Phi(X, logits))
        return self.P
