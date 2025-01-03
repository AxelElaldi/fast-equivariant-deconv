import torch


class ComputeSHC(torch.nn.Module):
    """Extract the spherical harmonic coefficient.
    """
    def __init__(self, S2SH):
        """Initialization.
        Args:
            S2SH (:obj:`torch.Tensor`): [V x C] Signal to spherical harmomic matrix
        """
        super(ComputeSHC, self).__init__()
        self.register_buffer('S2SH', S2SH)

    def forward(self, x):
        """Forward Pass.
        Args:
            x (:obj:`torch.Tensor`): input to be forwarded. [B x in_channels x V x X x Y x Z]
        Returns:
            :obj:`torch.Tensor`: output [B x in_channels x C x X x Y x Z]
        """
        x = torch.einsum('ijklmn,kp->ijplmn', x, self.S2SH) # B x in_channels x C x X x Y x Z
        return x


class ComputeSignal(torch.nn.Module):
    """Extract the signal.
    """
    def __init__(self, SH2S):
        """Initialization.
        Args:
            2 (:obj:`torch.Tensor`): [C x V] Spherical harmomic to signal matrix
        """
        super(ComputeSignal, self).__init__()
        self.register_buffer('SH2S', SH2S)

    def forward(self, x):
        """Forward Pass.
        Args:
            x (:obj:`torch.Tensor`): input to be forwarded. [B x in_channels x C x X x Y x Z]
        Returns:
            :obj:`torch.Tensor`: output [B x in_channels x V x X x Y x Z]
        """
        x = torch.einsum('ijklmn,kp->ijplmn', x, self.SH2S) # B x in_channels x V x X x Y x Z
        return x


class ShellComputeSignal(torch.nn.Module):
    """Extract the spherical harmonic coefficient per shell.
    """
    def __init__(self, shellSampling):
        """Initialization.
        Args:
            shellSampling (:obj:`sampling.ShellSampling`): Shell sampling object
        """
        super(ShellComputeSignal, self).__init__()
        self.shellSampling = shellSampling
        self.S = len(shellSampling.shell_values)
        self.V = shellSampling.vectors.shape[0]
        for i, sampling in enumerate(shellSampling.sampling):
            self.register_buffer(f'SH2S_{i}', torch.Tensor(sampling.SH2S))
        
    def SH2S(self, i):
        return self.__getattr__('SH2S_'+str(i)) # C x V_i

    def forward(self, x):
        """Forward Pass.
        Args:
            x (:obj:`torch.Tensor`): input to be forwarded. [B x in_channels x S x C x X x Y x Z]
        Returns:
            :obj:`torch.Tensor`: output [B x in_channels x V x X x Y x Z]
        """
        y = x.new_zeros((x.shape[0], x.shape[1], self.V, x.shape[4], x.shape[5], x.shape[6])) # B x in_channels x V x X x Y x Z
        C = x.shape[3]
        for i, sampling in enumerate(self.shellSampling.sampling):
            y[:, :, self.shellSampling.shell_inverse == i] = torch.einsum('ijklmn,kp->ijplmn', x[:, :, i], self.SH2S(i)[:C]) # B x in_channels x V_i x X x Y x Z
        return y


class ShellComputeSHC(torch.nn.Module):
    """Extract the spherical harmonic coefficient per shell.
    """
    def __init__(self, shellSampling):
        """Initialization.
        Args:
            shellSampling (:obj:`sampling.ShellSampling`): Shell sampling object
        """
        super(ShellComputeSHC, self).__init__()
        self.shellSampling = shellSampling
        self.S = len(self.shellSampling)
        for i, sampling in enumerate(shellSampling):
            self.register_buffer(f'S2SH_{i}', torch.Tensor(sampling.S2SH))
        self.C = sampling.S2SH.shape[1]
        
    def S2SH(self, i):
        return self.__getattr__('S2SH_'+str(i))

    def forward(self, x):
        """Forward Pass.
        Args:
            x (:obj:`torch.Tensor`): input to be forwarded. [B x in_channels x V x X x Y x Z]
        Returns:
            :obj:`torch.Tensor`: output [B x in_channels x S x C x X x Y x Z]
        """
        y = x.new_zeros((x.shape[0], x.shape[1], self.S, self.C, x.shape[3], x.shape[4], x.shape[5])) # B x in_channels x S x C x X x Y x Z
        for i, sampling in enumerate(self.shellSampling.sampling):
            y[:, :, i] = torch.einsum('ijklmn,kp->ijplmn', x[:, :, self.shellSampling.shell_inverse == i], self.S2SH(i)) # B x in_channels x C x X x Y x Z
        return y


def invariant_attr_r3s2_fiber_bundle(pos, ori_grid, edge_index):
    pos = torch.Tensor(pos)
    ori_grid = torch.Tensor(ori_grid)
    
    pos_send, pos_receive = pos[edge_index[0]], pos[edge_index[1]]                # [num_edges, 3]
    rel_pos = (pos_send - pos_receive)                                            # [num_edges, 3]

    # Convenient shape
    rel_pos = rel_pos[:, None, :]                                                 # [num_edges, 1, 3]
    ori_grid_a = ori_grid[None,:,:]                                               # [1, num_ori, 3]
    ori_grid_b = ori_grid[:, None,:]                                              # [num_ori, 1, 3]

    invariant1 = (rel_pos * ori_grid_a).sum(dim=-1, keepdim=True)                 # [num_edges, num_ori, 1]
    invariant2 = (rel_pos - invariant1 * ori_grid_a).norm(dim=-1, keepdim=True)   # [num_edges, num_ori, 1]
    invariant3 = (ori_grid_a * ori_grid_b).sum(dim=-1, keepdim=True)              # [num_ori, num_ori, 1]
    
    # Note: We could apply the acos = pi/2 - asin, which is differentiable at -1 and 1
    # But found that this mapping is unnecessary as it is monotonic and mostly linear 
    # anyway, except close to -1 and 1. Not applying the arccos worked just as well.
    # invariant3 = torch.pi / 2 - torch.asin(invariant3.clamp(-1.,1.))
    return torch.cat([invariant1, invariant2],dim=-1), invariant3   