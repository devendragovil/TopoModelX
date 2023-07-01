"""Cell Attention Network layer."""

import torch
from torch import Tensor
from torch.nn import Linear, Parameter
from torch.nn import functional as F

from topomodelx.base.aggregation import Aggregation
from topomodelx.base.conv import MessagePassing
from topomodelx.utils.scatter import scatter_sum


class MultiHeadCellAttention(MessagePassing):
    """Attentional Message Passing from Cell Attention Network (CAN) [CAN22]_.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    heads : int
        Number of attention heads.
    concat : bool
        Whether to concatenate the output of each attention head.
    att_activation : Callable
        Activation function to use for the attention weights.
    aggr_func : string
        Aggregation function to use. Options are "sum", "mean", "max".
    initialization : string
        Initialization method for the weights of the layer.

    Notes
    -----
    [] If there are no non-zero values in the neighborhood, then the neighborhood is empty.
    [] Add in utils add_self_loops function
    [] Add in utils softmax function

    References
    ----------
    [CAN22] Giusti, Battiloro, Testa, Di Lorenzo, Sardellitti and Barbarossa. “Cell attention networks”. In: arXiv preprint arXiv:2209.08179 (2022).
        paper: https://arxiv.org/pdf/2209.08179.pdf
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        dropout: float,
        heads: int,
        concat: bool,
        att_activation: torch.nn.Module,
        add_self_loops: bool = False,
        aggr_func: str = "sum",
        initialization: str = "xavier_uniform",
    ):
        super().__init__(
            att=True,
            initialization=initialization,
            aggr_func=aggr_func,
        )

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.att_activation = att_activation
        self.heads = heads
        self.concat = concat
        self.dropout = dropout

        if not add_self_loops:
            self.add_self_loops = None

        self.lin = torch.nn.Linear(in_channels, heads * out_channels, bias=False)
        self.att_weight_src = Parameter(torch.Tensor(1, heads, out_channels))
        self.att_weight_dst = Parameter(torch.Tensor(1, heads, out_channels))

        self.reset_parameters()

    def reset_parameters(self):
        """Reset the layer parameters."""
        torch.nn.init.xavier_uniform_(self.att_weight_src)
        torch.nn.init.xavier_uniform_(self.att_weight_dst)
        self.lin.reset_parameters()

    def attention(self, x_source, x_target):
        """Compute attention weights for messages.

        Parameters
        ----------
        x_source : torch.Tensor
            Source node features. Shape: [n_k_cells, in_channels]
        x_target : torch.Tensor
            Target node features. Shape: [n_k_cells, in_channels]

        Returns
        -------
        _ : torch.Tensor
            Attention weights. Shape: [n_k_cells, heads]
        """
        # Compute attention coefficients
        alpha_src = torch.einsum(
            "ijk,tjk->ij", x_source, self.att_weight_src
        )  # (|n_k_cells|, H)
        alpha_dst = torch.einsum(
            "ijk,tjk->ij", x_target, self.att_weight_dst
        )  # (|n_k_cells|, H)

        alpha = alpha_src + alpha_dst

        # Apply activation function
        alpha = self.att_activation(alpha)

        # Normalize the attention coefficients
        alpha = self.softmax(alpha, self.target_index_i, x_source.shape[0])

        # Apply dropout
        alpha = F.dropout(alpha, p=self.dropout, training=self.training)

        return alpha  # (|n_k_cells|, H)

    def softmax(self, src, index, num_cells):
        """Compute the softmax of the attention coefficients.

        Notes
        -----
        There should be of a default implementation of softmax in the utils file.
        Subtracting the maximum element in it from all elements to avoid overflow and underflow.

        Parameters
        ----------
        src : torch.Tensor
            Attention coefficients. Shape: [n_k_cells, heads]
        index : torch.Tensor
            Indices of the target nodes. Shape: [n_k_cells]
        num_cells : int
            Number of cells in the batch.

        Returns
        -------
        _ : torch.Tensor
            Softmax of the attention coefficients. Shape: [n_k_cells, heads]
        """
        src_max = src.max(dim=0, keepdim=True)[0]  # (1, H)
        src -= src_max  # (|n_k_cells|, H)
        src_exp = torch.exp(src)  # (|n_k_cells|, H)
        src_sum = scatter_sum(src_exp, index, dim=0, dim_size=num_cells)[
            index
        ]  # (|n_k_cells|, H)
        return src_exp / (src_sum + 1e-16)  # (|n_k_cells|, H)

    def add_self_loops(self, neighborhood):
        """Add self-loops to the neighborhood matrix.

        Parameters
        ----------
        neighborhood : torch.sparse_coo_tensor
            Neighborhood matrix. Shape: [n_k_cells, n_k_cells]

        Returns
        -------
        _ : torch.sparse_coo_tensor
            Neighborhood matrix with self-loops. Shape: [n_k_cells, n_k_cells]
        """
        N = neighborhood.shape[0]
        cell_index, cell_weight = neighborhood._indices(), neighborhood._values()
        # create loop index
        loop_index = torch.arange(0, N, dtype=torch.long, device=neighborhood.device)
        loop_index = loop_index.unsqueeze(0).repeat(2, 1)
        # add loop index to neighborhood
        cell_index = torch.cat([cell_index, loop_index], dim=1)
        cell_weight = torch.cat(
            [cell_weight, torch.ones(N, dtype=torch.float, device=neighborhood.device)]
        )

        return torch.sparse_coo_tensor(
            indices=cell_index,
            values=cell_weight,
            size=(N, N),
        ).coalesce()

    def forward(self, x_source, neighborhood):
        """Forward pass.

        Parameters
        ----------
        x_source : torch.Tensor, shape=[n_k_cells, channels]
            Input features on the k-cell of the cell complex.
        neighborhood : torch.sparse, shape=[n_k_cells, n_k_cells]
            Neighborhood matrix mapping k-cells to k-cells (A_k). [up, down]

        Returns
        -------
        _ : torch.Tensor, shape=[n_k_cells, channels]
            Output features on the k-cell of the cell complex.
        """
        # If there are no non-zero values in the neighborhood, then the neighborhood is empty. -> return zero tensor
        if not neighborhood.values().nonzero().size(0) > 0 and self.concat:
            return torch.zeros(
                (x_source.shape[0], self.out_channels * self.heads),
                device=x_source.device,
            )  # (n_k_cells, H * C)
        if not neighborhood.values().nonzero().size(0) > 0 and not self.concat:
            return torch.zeros(
                (x_source.shape[0], self.out_channels), device=x_source.device
            )  # (n_k_cells, C)

        # Compute the linear transformation on the source features
        x_message = self.lin(x_source).view(
            -1, self.heads, self.out_channels
        )  # (n_k_cells, H, C)

        # Add self-loops to the neighborhood matrix if necessary
        if self.add_self_loops is not None:
            # TODO: check if the self-loops are already added
            # TODO: should we remove the self-loops from the neighborhood matrix after the message passing?
            neighborhood = self.add_self_loops(neighborhood)

        # returns the indices of the non-zero values in the neighborhood matrix
        (
            self.target_index_i,
            self.source_index_j,
        ) = neighborhood.indices()  # (|n_k_cells|, 1), (|n_k_cells|, 1)

        # compute the source and target messages
        x_source_per_message = x_message[self.source_index_j]  # (|n_k_cells|, H, C)
        x_target_per_message = x_message[self.target_index_i]  # (|n_k_cells|, H, C)
        # compute the attention coefficients
        alpha = self.attention(
            x_source_per_message, x_target_per_message
        )  # (|n_k_cells|, H)

        # for each head, Aggregate the messages
        message = x_source_per_message * alpha[:, :, None]  # (|n_k_cells|, H, C)
        aggregated_message = self.aggregate(message)  # (n_k_cells, H, C)

        # if concat true, concatenate the messages for each head. Otherwise, average the messages for each head.
        if self.concat:
            return aggregated_message.view(
                -1, self.heads * self.out_channels
            )  # (n_k_cells, H * C)

        return aggregated_message.mean(dim=1)  # (n_k_cells, C)


class CANLayer(torch.nn.Module):
    r"""Layer of the Cell Attention Network (CAN) model.

    The CAN layer considers an attention convolutional message passing though the upper and lower neighborhoods of the cell.
    Additionally a skip connection can be added to the output of the layer.

    ..  math::
        \mathcal N_k \in  \mathcal N = \{A_{\uparrow, r}, A_{\downarrow, r}\}

    ..  math::
        \begin{align*}
        &🟥 \quad m_{y \rightarrow x}^{(r \rightarrow r)} = M_{\mathcal N_k}(h_x^{t}, h_y^{t}, \Theta^{t}_k)\\
        &🟧 \quad m_x^{(r \rightarrow r)} = \text{AGG}_{y \in \mathcal{N}_k(x)}(m_{y \rightarrow x}^{(r \rightarrow r)})\\
        &🟩 \quad m_x^{(r)} = \text{AGG}_{\mathcal{N}_k\in\mathcal N}m_x^{(r \rightarrow r)}\\
        &🟦 \quad h_x^{t+1,(r)} = U^{t}(h_x^{t}, m_x^{(r)})
        \end{align*}

    References
    ----------
    .. [CAN22] Giusti, Battiloro, Testa, Di Lorenzo, Sardellitti and Barbarossa.
        Cell attention networks.
        (2022) paper: https://arxiv.org/pdf/2209.08179.pdf

    Notes
    -----
    Add_self_loops is preferred to be False. If necessary, the self-loops should be added to the neighborhood matrix in the preprocessing step.

    Parameters
    ----------
    in_channels : int
        Dimension of input features on n-cells.
    out_channels : int
        Dimension of output
    heads : int, optional
        Number of attention heads, by default 1
    dropout : float, optional
        Dropout probability of the normalized attention coefficients, by default 0.0
    concat : bool, optional
        If True, the output of each head is concatenated. Otherwise, the output of each head is averaged, by default True
    skip_connection : bool, optional
        If True, skip connection is added, by default True
    add_self_loops : bool, optional
        If True, self-loops are added to the neighborhood matrix, by default False
    att_activation : Callable, optional
        Activation function applied to the attention coefficients, by default torch.nn.LeakyReLU()
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        heads: int = 1,
        dropout: float = 0.0,
        concat: bool = True,
        skip_connection: bool = True,
        att_activation: torch.nn.Module = torch.nn.LeakyReLU(),
        add_self_loops: bool = False,
        aggr_func="sum",
        update_func: str = "relu",
        **kwargs,
    ):
        super().__init__()

        assert in_channels > 0, ValueError("Number of input channels must be > 0")
        assert out_channels > 0, ValueError("Number of output channels must be > 0")
        assert heads > 0, ValueError("Number of heads must be > 0")
        assert dropout >= 0.0 and dropout <= 1.0, ValueError("Dropout must be in [0,1]")

        # lower attention
        self.lower_att = MultiHeadCellAttention(
            in_channels=in_channels,
            out_channels=out_channels,
            add_self_loops=add_self_loops,
            dropout=dropout,
            heads=heads,
            att_activation=att_activation,
            concat=concat,
        )

        # upper attention
        self.upper_att = MultiHeadCellAttention(
            in_channels=in_channels,
            out_channels=out_channels,
            add_self_loops=add_self_loops,
            dropout=dropout,
            heads=heads,
            att_activation=att_activation,
            concat=concat,
        )

        # linear transformation
        if skip_connection:
            out_channels = out_channels * heads if concat else out_channels
            self.lin = Linear(in_channels, out_channels, bias=False)
            self.eps = 1 + 1e-6

        # between-neighborhood aggregation and update
        self.aggregation = Aggregation(aggr_func=aggr_func, update_func=update_func)

        self.reset_parameters()

    def reset_parameters(self):
        """Reset the parameters of the layer."""
        self.lower_att.reset_parameters()
        self.upper_att.reset_parameters()
        if hasattr(self, "lin"):
            self.lin.reset_parameters()

    def forward(self, x, lower_neighborhood, upper_neighborhood) -> Tensor:
        """Forward pass.

        Parameters
        ----------
        x : torch.Tensor, shape=[n_k_cells, channels]
            Input features on the k-cell of the cell complex.
        lower_neighborhood : torch.sparse
            shape=[n_k_cells, n_k_cells]
            Lower neighborhood matrix mapping k-cells to k-cells (A_k_low).
        upper_neighborhood : torch.sparse
            shape=[n_k_cells, n_k_cells]
            Upper neighborhood matrix mapping k-cells to k-cells (A_k_up).

        Returns
        -------
        _ : torch.Tensor, shape=[n_k_cells, out_channels]
        """
        # message and within-neighborhood aggregation
        lower_x = self.lower_att(x, lower_neighborhood)
        upper_x = self.upper_att(x, upper_neighborhood)

        # skip connection
        if hasattr(self, "lin"):
            w_x = self.lin(x) * self.eps

        # between-neighborhood aggregation and update
        out = (
            self.aggregation([lower_x, upper_x, w_x])
            if hasattr(self, "lin")
            else self.aggregation([lower_x, upper_x])
        )

        return out
