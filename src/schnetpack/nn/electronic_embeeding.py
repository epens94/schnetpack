import torch
import torch.nn as nn
import torch.nn.functional as F

from typing import Optional
from schnetpack.nn.blocks import ResidualMLP

class ElectronicEmbedding(nn.Module):
    """
    Single Head self attention like block for updating atomic features through nonlocal interactions with the
    electrons.
    The embeddings are used to map the total molecular charge or molecular spin to a feature vector.
    Since those properties are not localized on a specific atom they have to be delocalized over the whole molecule.
    The delocalization is achieved by using a self attention like mechanism.


    Arguments:
        num_features (int):
            Dimensions of feature space aka the number of features to describe atomic environments.
            This determines the size of each embedding vector
        num_residual (int):
            Number of residual blocks applied to atomic features
        activation (str):
            Kind of activation function. Possible value:
            'ssp': Shifted softplus activation function.
        is_charged (bool):
            is_charged True corresponds to building embedding for molecular charge and
            separate weights are used for positive and negative charges.
            i_charged False corresponds to building embedding for spin values,
            no seperate weights are used
    """

    def __init__(
        self,
        num_features: int,
        num_residual: int,
        activation: str = "ssp",
        is_charged: bool = False,
    ) -> None:
        """ Initializes the ElectronicEmbedding class. """
        super(ElectronicEmbedding, self).__init__()
        self.is_charged = is_charged
        self.linear_q = nn.Linear(num_features, num_features)
        if is_charged:  # charges are duplicated to use separate weights for +/-
            self.linear_k = nn.Linear(2, num_features, bias=False)
            self.linear_v = nn.Linear(2, num_features, bias=False)
        else:
            self.linear_k = nn.Linear(1, num_features, bias=False)
            self.linear_v = nn.Linear(1, num_features, bias=False)
        self.resblock = ResidualMLP(
            num_features,
            num_residual,
            activation=activation,
            zero_init=True,
            bias=False,
        )
        self.reset_parameters()

    def reset_parameters(self) -> None:
        """ Initialize parameters. """
        nn.init.orthogonal_(self.linear_k.weight)
        nn.init.orthogonal_(self.linear_v.weight)
        nn.init.orthogonal_(self.linear_q.weight)
        nn.init.zeros_(self.linear_q.bias)

    def forward(
        self,
        atomic_features: torch.Tensor,
        electronic_feature: torch.Tensor,
        num_batch: int,
        batch_seg: torch.Tensor,
        eps: float = 1e-8,
    ) -> torch.Tensor:
        """
        Evaluate interaction block.

        atomic_features (FloatTensor [N, num_features]):
            Atomic feature vectors.
        electronic_feature (FloatTensor [N]): 
            either charges or spin values per molecular graph
        num_batch (int): 
            number of molecular graphs in the batch
        batch_seq (LongTensor [N]): 
            segment ids (aka _idx_m) are used to separate different molecules in a batch
        eps (float): 
            small number to avoid division by zero
        """
        
        # queries (Batchsize x N_atoms, n_atom_basis)
        q = self.linear_q(atomic_features) 
        
        # to account for negative and positive charge
        if self.is_charged:
            e = F.relu(torch.stack([electronic_feature, -electronic_feature], dim=-1))
        # +/- spin is the same => abs
        else:
            e = torch.abs(electronic_feature).unsqueeze(-1)  
        enorm = torch.maximum(e, torch.ones_like(e))

        # keys (Batchsize x N_atoms, n_atom_basis), the batch_seg ensures that the key is the same for all atoms belonging to the same graph
        k = self.linear_k(e / enorm)[batch_seg] 

        # values (Batchsize x N_atoms, n_atom_basis) the batch_seg ensures that the value is the same for all atoms belonging to the same graph
        v = self.linear_v(e)[batch_seg]

        # unnormalized, scaled attention weights, obtained by dot product of queries and keys (are logits)
        # scaling by square root of attention dimension
        weights = torch.sum(k * q, dim=-1) / k.shape[-1] ** 0.5

        # probability distribution of scaled unnormalized attention weights, by applying softmax function
        a = nn.functional.softplus(weights)

        # normalization factor for every molecular graph, by adding up attention weights of every atom in the graph
        anorm = a.new_zeros(num_batch).index_add_(0, batch_seg, a)
        
        # make tensor filled with anorm value at the position of the corresponding molecular graph, 
        # indexing faster on CPU, gather faster on GPU
        if a.device.type == "cpu": 
            anorm = anorm[batch_seg]
        else:
            anorm = torch.gather(anorm, 0, batch_seg)
        
        # return probability distribution of scaled normalized attention weights, eps is added for numerical stability (sum / batchsize equals 1)
        return self.resblock((a / (anorm + eps)).unsqueeze(-1) * v)