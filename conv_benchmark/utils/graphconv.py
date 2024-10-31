import torch
import math
import numpy as np
from .utils_equivariance import invariant_attr_r3s2_fiber_bundle

class Conv(torch.nn.Module):
    """Building Block with a Chebyshev Convolution.
    """

    def __init__(self, in_channels, out_channels, lap, kernel_sizeSph=3, kernel_sizeSpa=3, bias=True, conv_name='spherical', isoSpa=True, dense=False, precompute=False, einsum=False, repeat_interleave=False):
        """Initialization.
        Args:
            in_channels (int): initial number of channels
            out_channels (int): output number of channels
            lap (:obj:`torch.sparse.FloatTensor`): laplacian
            kernel_sizeSph (int): Number of trainable parameters per filter, which is also the size of the convolutional kernel.
                                The order of the Chebyshev polynomials is kernel_size - 1. Defaults to 3.
            kernel_sizeSpa (int): Size of the spatial filter.
            bias (bool): Whether to add a bias term.
            conv_name (str): Name of the convolution, either 'spherical' or 'mixed'
        """
        super(Conv, self).__init__()
        verbose = False
        self.einsum = einsum
        if verbose:
            print(f'Add convolution: {conv_name} - in_channels: {in_channels} - out_channels: {out_channels} - lap: {lap.shape} - kernel_sizeSph: {kernel_sizeSph} - kernel_sizeSpa: {kernel_sizeSpa} - isoSpa: {isoSpa}')
        if dense:
            lap = lap.to_dense()
        self.precompute = precompute
        if precompute:
            projector = precompute_projection(lap, kernel_sizeSph)
            if not einsum:
                projector = projector.permute(0, 2, 1).contiguous().view(kernel_sizeSph*lap.shape[1], lap.shape[0])
            self.register_buffer("laplacian", projector)
        else:
            self.register_buffer("laplacian", lap)
        if conv_name == 'spherical':
            self.conv = ChebConv(in_channels, out_channels, kernel_sizeSph, bias, dense=dense)
        elif conv_name == 'mixed':
            self.conv = SO3SE3Conv(in_channels, out_channels, kernel_sizeSph, kernel_sizeSpa, bias, isoSpa=isoSpa, dense=dense, repeat_interleave=repeat_interleave)
        elif conv_name in ['spatial', 'spatial_vec', 'spatial_sh']:
            self.conv = SpatialConv(in_channels, out_channels, kernel_sizeSpa, bias, isoSpa=isoSpa)
        else:
            raise NotImplementedError

    def state_dict(self, *args, **kwargs):
        """! WARNING !
        This function overrides the state dict in order to be able to save the model.
        This can be removed as soon as saving sparse matrices has been added to Pytorch.
        """
        state_dict = super().state_dict(*args, **kwargs)
        del_keys = []
        for key in state_dict:
            if key.endswith("laplacian"):
                del_keys.append(key)
        for key in del_keys:
            del state_dict[key]
        return state_dict

    def forward(self, x):
        """Forward pass.
        Args:
            x (:obj:`torch.tensor`): input [B x Fin x V x X x Y x Z]
        Returns:
            :obj:`torch.tensor`: output [B x Fout x V x X x Y x Z]
        """
        x = self.conv(self.laplacian, x, self.precompute, self.einsum)
        return x


##### CHEB CONV ######
class ChebConv(torch.nn.Module):
    """Graph convolutional layer.
    """
    def __init__(self, in_channels, out_channels, kernel_size, bias=True, dense=False):
        """Initialize the Chebyshev layer.
        Args:
            in_channels (int): Number of channels/features in the input graph.
            out_channels (int): Number of channels/features in the output graph.
            kernel_size (int): Number of trainable parameters per filter, which is also the size of the convolutional kernel.
                                The order of the Chebyshev polynomials is kernel_size - 1.
            bias (bool): Whether to add a bias term.
        """
        super(ChebConv, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self._conv = cheb_conv_dense if dense else cheb_conv

        shape = (kernel_size, in_channels, out_channels)
        self.weight = torch.nn.Parameter(torch.Tensor(*shape))
        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter("bias", None)

        self.kaiming_initialization()

    def kaiming_initialization(self):
        """Initialize weights and bias.
        """
        std = math.sqrt(2 / (self.in_channels * self.kernel_size))
        #std = 1e-1
        self.weight.data.normal_(0, std)
        if self.bias is not None:
            self.bias.data.fill_(0.01)

    def forward(self, laplacian, inputs, precompute, einsum):
        """Forward graph convolution.
        Args:
            laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
            inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        Returns:
            :obj:`torch.Tensor`: The convoluted inputs.
        """
        outputs = self._conv(laplacian, inputs, self.weight, precompute, einsum)
        if self.bias is not None:
            outputs += self.bias[None, :, None, None, None, None]
        return outputs


def cheb_conv(laplacian, inputs, weight, precompute=False, einsum=False):
    """Chebyshev convolution.
    Args:
        laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
        inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        weight (:obj:`torch.Tensor`): The weights of the current layer.
    Returns:
        :obj:`torch.Tensor`: Inputs after applying Chebyshev convolution.
    """
    # Get tensor dimensions
    B, Fin, V, X, Y, Z = inputs.shape
    K, Fin, Fout = weight.shape
    # B = batch size
    # V = nb vertices
    # Fin = nb input features
    # Fout = nb output features
    # K = order of Chebyshev polynomials + 1

    # Transform to Chebyshev basis
    if precompute:
        if einsum:
            inputs = torch.einsum('bfvxyz,kvw->bfkwxyz', inputs, laplacian)  # B x Fin x K x V x X x Y x Z
        else:
            #x0 = inputs.permute(2, 1, 0, 3, 4, 5).contiguous()  # V x Fin x B x X x Y x Z
            #x0 = x0.view([V, Fin * B * X * Y * Z])  # V x Fin*B*X*Y*Z
            #inputs = torch.mm(laplacian, x0) # K*V x Fin*B*X*Y*Z
            #inputs = inputs.view([K, V, Fin, B, X, Y, Z])  # K x V x Fin x B x X x Y x Z
            #inputs = inputs.permute(3, 1, 4, 5, 6, 0, 2).contiguous()  # B x V x X x Y x Z x K x Fin
            x0 = inputs.permute(2, 1, 0, 3, 4, 5).reshape([V, Fin * B * X * Y * Z]) # V x Fin*B*X*Y*Z
            inputs = torch.mm(laplacian, x0)  # K*V x Fin*B*X*Y*Z
            inputs = inputs.reshape([K, V, Fin, B, X, Y, Z]).permute(3, 1, 4, 5, 6, 0, 2)  # B x V x X x Y x Z x K x Fin
    else:
        x0 = inputs.permute(2, 1, 0, 3, 4, 5).contiguous()  # V x Fin x B x X x Y x Z
        x0 = x0.view([V, Fin * B * X * Y * Z])  # V x Fin*B*X*Y*Z
        inputs = project_cheb_basis(laplacian, x0, K) # K x V x Fin*B*X*Y*Z
        inputs = inputs.view([K, V, Fin, B, X, Y, Z])  # K x V x Fin x B x X x Y x Z
        inputs = inputs.permute(3, 1, 4, 5, 6, 0, 2).contiguous()  # B x V x X x Y x Z x K x Fin

    if einsum:
        weight = weight.permute(1, 0, 2).contiguous().view(K * Fin, Fout) # K*Fin x Fout
        inputs = inputs.reshape([B, Fin * K, V, X, Y, Z])  # B x Fin*K x V x X x Y x Z
        inputs = torch.einsum('bfvxyz,fg->bgvxyz', inputs, weight) # B x Fout x V x X x Y x Z
    else:
        #inputs = inputs.view([B * V * X * Y * Z, K * Fin])  # B*V*X*Y*Z x K*Fin
        #weight = weight.view(K * Fin, Fout) # K*Fin x Fout
        #inputs = torch.mm(inputs, weight)  # B*V*X*Y*Z x Fout
        #inputs = inputs.view([B, V, X, Y, Z, Fout])  # B x V x X x Y x Z x Fout
        #inputs = inputs.permute(0, 5, 1, 2, 3, 4).contiguous()  # B x Fout x V x X x Y x Z
        inputs = inputs.view([B * V * X * Y * Z, K * Fin])  # B*V*X*Y*Z x K*Fin
        weight = weight.view(K * Fin, Fout) # K*Fin x Fout
        inputs = torch.mm(inputs, weight)  # B*V*X*Y*Z x Fout
        inputs = inputs.reshape([B, V, X, Y, Z, Fout]).permute(0, 5, 1, 2, 3, 4)  # B x Fout x V x X x Y x Z
    return inputs


def cheb_conv_dense(laplacian, inputs, weight, precompute=False, einsum=False):
    """Chebyshev convolution.
    Args:
        laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
        inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        weight (:obj:`torch.Tensor`): The weights of the current layer.
    Returns:
        :obj:`torch.Tensor`: Inputs after applying Chebyshev convolution.
    """
    # Get tensor dimensions
    B, Fin, V, X, Y, Z = inputs.shape
    K, Fin, Fout = weight.shape
    # B = batch size
    # V = nb vertices
    # Fin = nb input features
    # Fout = nb output features
    # K = order of Chebyshev polynomials + 1

    # Transform to Chebyshev basis
    if precompute:
        if einsum:
            inputs = torch.einsum('bfvxyz,kvw->bfkwxyz', inputs, laplacian)  # B x Fin x K x V x X x Y x Z
        else:
            #x0 = inputs.permute(2, 1, 0, 3, 4, 5).contiguous()  # V x Fin x B x X x Y x Z
            #x0 = x0.view([V, Fin * B * X * Y * Z])  # V x Fin*B*X*Y*Z
            #inputs = torch.mm(laplacian, x0) # K*V x Fin*B*X*Y*Z
            #inputs = inputs.view([K, V, Fin, B, X, Y, Z])  # K x V x Fin x B x X x Y x Z
            #inputs = inputs.permute(3, 1, 4, 5, 6, 0, 2).contiguous()  # B x V x X x Y x Z x K x Fin
            x0 = inputs.permute(2, 1, 0, 3, 4, 5).reshape([V, Fin * B * X * Y * Z]) # V x Fin*B*X*Y*Z
            inputs = torch.mm(laplacian, x0)  # K*V x Fin*B*X*Y*Z
            inputs = inputs.reshape([K, V, Fin, B, X, Y, Z]).permute(3, 1, 4, 5, 6, 0, 2)  # B x V x X x Y x Z x K x Fin
    else:
        x0 = inputs.permute(2, 1, 0, 3, 4, 5).contiguous()  # V x Fin x B x X x Y x Z
        x0 = x0.view([V, Fin * B * X * Y * Z])  # V x Fin*B*X*Y*Z
        inputs = project_cheb_basis_dense(laplacian, x0, K) # K x V x Fin*B*X*Y*Z
        inputs = inputs.view([K, V, Fin, B, X, Y, Z])  # K x V x Fin x B x X x Y x Z
        inputs = inputs.permute(3, 1, 4, 5, 6, 0, 2).contiguous()  # B x V x X x Y x Z x K x Fin

    if einsum:
        weight = weight.permute(1, 0, 2).contiguous().view(K * Fin, Fout) # K*Fin x Fout
        inputs = inputs.reshape([B, Fin * K, V, X, Y, Z])  # B x Fin*K x V x X x Y x Z
        inputs = torch.einsum('bfvxyz,fg->bgvxyz', inputs, weight) # B x Fout x V x X x Y x Z
    else:
        #inputs = inputs.view([B * V * X * Y * Z, K * Fin])  # B*V*X*Y*Z x K*Fin
        #weight = weight.view(K * Fin, Fout) # K*Fin x Fout
        #inputs = torch.mm(inputs, weight)  # B*V*X*Y*Z x Fout
        #inputs = inputs.view([B, V, X, Y, Z, Fout])  # B x V x X x Y x Z x Fout
        #inputs = inputs.permute(0, 5, 1, 2, 3, 4).contiguous()  # B x Fout x V x X x Y x Z
        inputs = inputs.reshape([B * V * X * Y * Z, K * Fin])  # B*V*X*Y*Z x K*Fin
        weight = weight.view(K * Fin, Fout) # K*Fin x Fout
        inputs = torch.mm(inputs, weight)  # B*V*X*Y*Z x Fout
        inputs = inputs.reshape([B, V, X, Y, Z, Fout]).permute(0, 5, 1, 2, 3, 4)  # B x Fout x V x X x Y x Z
    return inputs


#### SE3 x SO3 CONV #####
class SO3SE3Conv(torch.nn.Module):
    """Graph convolutional layer.
    """
    def __init__(self, in_channels, out_channels, kernel_sizeSph, kernel_sizeSpa, bias=True, isoSpa=True, dense=False, repeat_interleave=False):
        """Initialize the Chebyshev layer.
        Args:
            in_channels (int): Number of channels/features in the input graph.
            out_channels (int): Number of channels/features in the output graph.
            kernel_sizeSph (int): Number of trainable parameters per spherical filter, which is also the size of the convolutional kernel.
                                The order of the Chebyshev polynomials is kernel_size - 1.
            kernel_sizeSpa (int): Size of the spatial filter.
            bias (bool): Whether to add a bias term.
        """
        super(SO3SE3Conv, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_sizeSph = kernel_sizeSph
        self.kernel_sizeSpa = kernel_sizeSpa
        self.isoSpa = isoSpa
        self._conv = se3so3_conv_dense if dense else se3so3_conv
        self.repeat_interleave = repeat_interleave

        shape = (out_channels, in_channels, kernel_sizeSph)
        self.weightSph = torch.nn.Parameter(torch.Tensor(*shape))

        if self.isoSpa:
            weight_tmp, ind, distance = self.get_index(kernel_sizeSpa)
            self.register_buffer('weight_tmp', weight_tmp)
            self.ind = ind.reshape(kernel_sizeSpa, kernel_sizeSpa, kernel_sizeSpa)
            shape = (out_channels, in_channels, 1, 1, 1, self.weight_tmp.shape[-1])
        else:
            shape = (out_channels, in_channels, kernel_sizeSpa, kernel_sizeSpa, kernel_sizeSpa)
            
        self.weightSpa = torch.nn.Parameter(torch.Tensor(*shape))

        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter("bias", None)

        self.kaiming_initialization()

    def kaiming_initialization(self):
        """Initialize weights and bias.
        """
        std = math.sqrt(2 / (self.in_channels * self.kernel_sizeSph))
        #std = 1e-1
        self.weightSph.data.normal_(0, std)
        std = math.sqrt(2 / (self.in_channels * (self.kernel_sizeSpa**3)))
        #std = 1e-1
        self.weightSpa.data.normal_(0, std)
        if self.bias is not None:
            mean = 0.01
            #mean = 1e-3 # 0.01
            self.bias.data.fill_(mean)

    def get_index(self, size):
        x_mid = (size - 1)/2
        x = np.arange(size) - x_mid
        distance = np.sqrt(x[None, None, :]**2 + x[None, :, None]**2 + x[:, None, None]**2)
        unique, ind = np.unique(distance, return_inverse=True)
        weight_tmp = torch.zeros((self.out_channels, self.in_channels, size, size, size, len(unique)))
        for i in range(len(unique)):
            weight_tmp[:, :, :, :, :, i][:, :, torch.Tensor(ind.reshape((size, size, size))==i).type(torch.bool)] = 1
        return weight_tmp, ind, distance

    def forward(self, laplacian, inputs, precompute, einsum):
        """Forward graph convolution.
        Args:
            laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
            inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        Returns:
            :obj:`torch.Tensor`: The convoluted inputs.
        """
        if self.isoSpa:
            weight = torch.sum((self.weight_tmp * self.weightSpa), -1)
            outputs = self._conv(laplacian, inputs, self.weightSph, weight, precompute, einsum, self.repeat_interleave)
        else:
            outputs = self._conv(laplacian, inputs, self.weightSph, self.weightSpa, precompute, einsum, self.repeat_interleave)
        if self.bias is not None:
            outputs += self.bias[None, :, None, None, None, None]
        return outputs


def se3so3_conv(laplacian, inputs, weightSph, weightSpa, precompute, einsum, repeat_interleave):
    """SE(3) x SO(3) grid convolution.
    Args:
        laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
        inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        weightSph (:obj:`torch.Tensor`): The spherical weights of the current layer.
        weightSpa (:obj:`torch.Tensor`): The spatial weights of the current layer.
    Returns:
        :obj:`torch.Tensor`: Inputs after applying Chebyshev convolution.
    """
    # Get tensor dimensions
    B, Fin, V, X, Y, Z = inputs.shape
    Fout, Fin, K = weightSph.shape
    Fout, Fin, kX, kY, kZ = weightSpa.shape
    # B = batch size
    # V = nb vertices
    # Fin = nb input features
    # Fout = nb output features
    # K = order of Chebyshev polynomials + 1

    # Transform to Chebyshev basis
    if precompute:
        if einsum:
            # Transform to Chebyshev basis
            inputs = torch.einsum('bfvxyz,kvw->bfkwxyz', inputs, laplacian) # B x Fin x K x V x X x Y x Z
            inputs = inputs.permute(0, 3, 1, 2, 4, 5, 6) #.contiguous()  # B x V x Fin x K x X x Y x Z
            inputs = inputs.reshape([B * V, Fin * K, X, Y, Z])  # B*V x Fin*K x X x Y x Z
        else:
            x0 = inputs.permute(2, 1, 0, 3, 4, 5).contiguous()  # V x Fin x B x X x Y x Z
            x0 = x0.view([V, Fin * B * X * Y * Z])  # V x Fin*B*X*Y*Z
            inputs = torch.mm(laplacian, x0) # K*V x Fin*B*X*Y*Z
            inputs = inputs.view([K, V, Fin, B, X, Y, Z])  # K x V x Fin x B x X x Y x Z
            inputs = inputs.permute(3, 1, 2, 0, 4, 5, 6).contiguous()  # B x V x Fin x K x X x Y x Z
            inputs = inputs.view([B * V, Fin * K, X, Y, Z])  # B*V x Fin*K x X x Y x Z
    else:
        x0 = inputs.permute(2, 1, 0, 3, 4, 5).contiguous()  # V x Fin x B x X x Y x Z
        x0 = x0.view([V, Fin * B * X * Y * Z])  # V x Fin*B*X*Y*Z
        inputs = project_cheb_basis(laplacian, x0, K) # K x V x Fin*B*X*Y*Z

        # Look at the Chebyshev transforms as feature maps at each vertex
        inputs = inputs.view([K, V, Fin, B, X, Y, Z])  # K x V x Fin x B x X x Y x Z
        inputs = inputs.permute(3, 1, 2, 0, 4, 5, 6).contiguous()  # B x V x Fin x K x X x Y x Z
        inputs = inputs.view([B * V, Fin * K, X, Y, Z])  # B*V x Fin*K x X x Y x Z

    # Expand spherical and Spatial filters
    wSph = weightSph.view([Fout, Fin*K, 1, 1, 1]).expand(-1, -1, kX, kY, kZ) # Fout x Fin*K x kX x kY x kZ
    if repeat_interleave:
        wSpa = weightSpa.repeat_interleave(K, dim=1) # Fout x Fin*K x kX x kY x kZ
    else:
        wSpa = weightSpa[:, :, None].expand(-1, -1, K, -1, -1, -1).flatten(1, 2)
    weight = wSph * wSpa # Fout x Fin*K x kX x kY x kZ

    # Convolution
    inputs = torch.nn.functional.conv3d(inputs, weight, padding='same') # B*V x Fout x X x Y x Z

    # Get final output tensor
    inputs = inputs.view([B, V, Fout, X, Y, Z])  # B x V x Fout x X x Y x Z
    inputs = inputs.permute(0, 2, 1, 3, 4, 5).contiguous()  # B x Fout x V x X x Y x Z

    return inputs


#### SpatialConv #####
class SpatialConv(torch.nn.Module):
    """Graph convolutional layer.
    """
    def __init__(self, in_channels, out_channels, kernel_sizeSpa, bias=True, isoSpa=True):
        """Initialize the Chebyshev layer.
        Args:
            in_channels (int): Number of channels/features in the input graph.
            out_channels (int): Number of channels/features in the output graph.
            kernel_sizeSph (int): Number of trainable parameters per spherical filter, which is also the size of the convolutional kernel.
                                The order of the Chebyshev polynomials is kernel_size - 1.
            kernel_sizeSpa (int): Size of the spatial filter.
            bias (bool): Whether to add a bias term.
        """
        super(SpatialConv, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_sizeSpa = kernel_sizeSpa
        self.isoSpa = isoSpa

        if self.isoSpa:
            weight_tmp, ind, distance = self.get_index(kernel_sizeSpa)
            self.register_buffer('weight_tmp', weight_tmp)
            self.ind = ind.reshape(kernel_sizeSpa, kernel_sizeSpa, kernel_sizeSpa)
            shape = (out_channels, in_channels, 1, 1, 1, self.weight_tmp.shape[-1])
        else:
            shape = (out_channels, in_channels, kernel_sizeSpa, kernel_sizeSpa, kernel_sizeSpa)
        self.weightSpa = torch.nn.Parameter(torch.Tensor(*shape))

        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter("bias", None)

        self.kaiming_initialization()

    def kaiming_initialization(self):
        """Initialize weights and bias.
        """
        std = math.sqrt(2 / (self.in_channels * (self.kernel_sizeSpa**3)))
        self.weightSpa.data.normal_(0, std)
        if self.bias is not None:
            self.bias.data.fill_(0.01)

    def get_index(self, size):
        x_mid = (size - 1)/2
        x = np.arange(size) - x_mid
        distance = np.sqrt(x[None, None, :]**2 + x[None, :, None]**2 + x[:, None, None]**2)
        unique, ind = np.unique(distance, return_inverse=True)
        weight_tmp = torch.zeros((self.out_channels, self.in_channels, size, size, size, len(unique)))
        for i in range(len(unique)):
            weight_tmp[:, :, :, :, :, i][:, :, torch.Tensor(ind.reshape((size, size, size))==i).type(torch.bool)] = 1
        return weight_tmp, ind, distance

    def forward(self, laplacian, inputs, precompute, einsum):
        """Forward graph convolution.
        Args:
            laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
            inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        Returns:
            :obj:`torch.Tensor`: The convoluted inputs.
        """
        redim = False
        if len(inputs.shape)==6:
            redim = True
            B, Fin, V, X, Y, Z = inputs.shape
            inputs = inputs.reshape(B, Fin * V, X, Y, Z)
        if self.isoSpa:
            weight = torch.sum((self.weight_tmp * self.weightSpa), -1)
            outputs = torch.nn.functional.conv3d(inputs, weight, padding='same') # B x Fout x X x Y x Z
        else:
            outputs = torch.nn.functional.conv3d(inputs, self.weightSpa, padding='same') # B x Fout x X x Y x Z
        
        if self.bias is not None:
            outputs += self.bias[None, :, None, None, None]
        if redim:
            _, _, X, Y, Z = outputs.shape
            outputs = outputs.reshape(B, -1, V, X, Y, Z)
        return outputs



def project_cheb_basis(laplacian, x0, K):
    """Project vector x on the Chebyshev basis of order K
    \hat{x}_0 = x
    \hat{x}_1 = Lx
    \hat{x}_k = 2*L\hat{x}_{k-1} - \hat{x}_{k-2}
    Args:
        laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
        x0 (:obj:`torch.Tensor`): The initial data being forwarded. [V x D]
        K (:obj:`torch.Tensor`): The order of Chebyshev polynomials + 1.
    Returns:
        :obj:`torch.Tensor`: Inputs after applying Chebyshev projection.
    """
    inputs = x0.unsqueeze(0)  # 1 x V x D
    if K > 1:
        x1 = torch.sparse.mm(laplacian, x0)  # V x D
        inputs = torch.cat((inputs, x1.unsqueeze(0)), 0)  # 2 x V x D
        for _ in range(2, K):
            x2 = 2 * torch.sparse.mm(laplacian, x1) - x0
            inputs = torch.cat((inputs, x2.unsqueeze(0)), 0)  # _ x V x D
            x0, x1 = x1, x2
    return inputs # K x V x D

def project_cheb_basis_dense(laplacian, x0, K):
    """Project vector x on the Chebyshev basis of order K
    \hat{x}_0 = x
    \hat{x}_1 = Lx
    \hat{x}_k = 2*L\hat{x}_{k-1} - \hat{x}_{k-2}
    Args:
        laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
        x0 (:obj:`torch.Tensor`): The initial data being forwarded. [V x D]
        K (:obj:`torch.Tensor`): The order of Chebyshev polynomials + 1.
    Returns:
        :obj:`torch.Tensor`: Inputs after applying Chebyshev projection.
    """
    inputs = x0.unsqueeze(0)  # 1 x V x D
    if K > 1:
        x1 = torch.mm(laplacian, x0)  # V x D
        inputs = torch.cat((inputs, x1.unsqueeze(0)), 0)  # 2 x V x D
        for _ in range(2, K):
            x2 = 2 * torch.mm(laplacian, x1) - x0
            inputs = torch.cat((inputs, x2.unsqueeze(0)), 0)  # _ x V x D
            x0, x1 = x1, x2
    return inputs # K x V x D


def se3so3_conv_dense(laplacian, inputs, weightSph, weightSpa, precompute, einsum, repeat_interleave):
    """SE(3) x SO(3) grid convolution.
    Args:
        laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
        inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        weightSph (:obj:`torch.Tensor`): The spherical weights of the current layer.
        weightSpa (:obj:`torch.Tensor`): The spatial weights of the current layer.
    Returns:
        :obj:`torch.Tensor`: Inputs after applying Chebyshev convolution.
    """
    # Get tensor dimensions
    B, Fin, V, X, Y, Z = inputs.shape
    Fout, Fin, K = weightSph.shape
    Fout, Fin, kX, kY, kZ = weightSpa.shape
    # B = batch size
    # V = nb vertices
    # Fin = nb input features
    # Fout = nb output features
    # K = order of Chebyshev polynomials + 1

    # Transform to Chebyshev basis
    if precompute:
        if einsum:
            # Transform to Chebyshev basis
            inputs = torch.einsum('bfvxyz,kvw->bfkwxyz', inputs, laplacian) # B x Fin x K x V x X x Y x Z
            inputs = inputs.permute(0, 3, 1, 2, 4, 5, 6) #.contiguous()  # B x V x Fin x K x X x Y x Z
            inputs = inputs.reshape([B * V, Fin * K, X, Y, Z])  # B*V x Fin*K x X x Y x Z
        else:
            x0 = inputs.permute(2, 1, 0, 3, 4, 5).contiguous()  # V x Fin x B x X x Y x Z
            x0 = x0.view([V, Fin * B * X * Y * Z])  # V x Fin*B*X*Y*Z
            inputs = torch.mm(laplacian, x0) # K*V x Fin*B*X*Y*Z
            inputs = inputs.view([K, V, Fin, B, X, Y, Z])  # K x V x Fin x B x X x Y x Z
            inputs = inputs.permute(3, 1, 2, 0, 4, 5, 6).contiguous()  # B x V x Fin x K x X x Y x Z
            inputs = inputs.view([B * V, Fin * K, X, Y, Z])  # B*V x Fin*K x X x Y x Z
    else:
        x0 = inputs.permute(2, 1, 0, 3, 4, 5).contiguous()  # V x Fin x B x X x Y x Z
        x0 = x0.view([V, Fin * B * X * Y * Z])  # V x Fin*B*X*Y*Z
        inputs = project_cheb_basis_dense(laplacian, x0, K) # K x V x Fin*B*X*Y*Z

        # Look at the Chebyshev transforms as feature maps at each vertex
        inputs = inputs.view([K, V, Fin, B, X, Y, Z])  # K x V x Fin x B x X x Y x Z
        inputs = inputs.permute(3, 1, 2, 0, 4, 5, 6).contiguous()  # B x V x Fin x K x X x Y x Z
        inputs = inputs.view([B * V, Fin * K, X, Y, Z])  # B*V x Fin*K x X x Y x Z

    # Expand spherical and Spatial filters
    wSph = weightSph.view([Fout, Fin*K, 1, 1, 1]).expand(-1, -1, kX, kY, kZ) # Fout x Fin*K x kX x kY x kZ
    if repeat_interleave:
        wSpa = weightSpa.repeat_interleave(K, dim=1) # Fout x Fin*K x kX x kY x kZ
    else:
        wSpa = weightSpa[:, :, None].expand(-1, -1, K, -1, -1, -1).flatten(1, 2)
    weight = wSph * wSpa # Fout x Fin*K x kX x kY x kZ

    # Convolution
    inputs = torch.nn.functional.conv3d(inputs, weight, padding='same') # B*V x Fout x X x Y x Z

    # Get final output tensor
    inputs = inputs.view([B, V, Fout, X, Y, Z])  # B x V x Fout x X x Y x Z
    inputs = inputs.permute(0, 2, 1, 3, 4, 5).contiguous()  # B x Fout x V x X x Y x Z

    return inputs

class ConvPrecomputed(torch.nn.Module):
    """Building Block with a Chebyshev Convolution.
    """

    def __init__(self, in_channels, out_channels, lap, kernel_sizeSph=3, kernel_sizeSpa=3, bias=True, conv_name='spherical', isoSpa=True, dense=False, vec=None):
        """Initialization.
        Args:
            in_channels (int): initial number of channels
            out_channels (int): output number of channels
            lap (:obj:`torch.sparse.FloatTensor`): laplacian
            kernel_sizeSph (int): Number of trainable parameters per filter, which is also the size of the convolutional kernel.
                                The order of the Chebyshev polynomials is kernel_size - 1. Defaults to 3.
            kernel_sizeSpa (int): Size of the spatial filter.
            bias (bool): Whether to add a bias term.
            conv_name (str): Name of the convolution, either 'spherical' or 'mixed'
        """
        super(ConvPrecomputed, self).__init__()
        verbose = False
        if verbose:
            print(f'Add convolution: {conv_name} - in_channels: {in_channels} - out_channels: {out_channels} - lap: {lap.shape} - kernel_sizeSph: {kernel_sizeSph} - kernel_sizeSpa: {kernel_sizeSpa} - isoSpa: {isoSpa}')
        if conv_name == 'spherical':
            self.conv = ChebConvPrecomputed(lap, in_channels, out_channels, kernel_sizeSph, bias, dense=dense)
        elif conv_name == 'mixed':
            self.conv = SO3SE3ConvPrecomputer(lap, in_channels, out_channels, kernel_sizeSph, kernel_sizeSpa, bias, isoSpa=isoSpa, dense=dense)
        elif conv_name in ['spatial', 'spatial_vec', 'spatial_sh']:
            self.conv = SpatialConvPrecomputed(in_channels, out_channels, kernel_sizeSpa, bias, isoSpa=isoSpa)
        elif conv_name == 'bekkers':
            #################################
            kernel_size = kernel_sizeSpa
            xx = np.arange(kernel_size) - kernel_size//2 + ((kernel_size+1)%2)*0.5
            yy = np.arange(kernel_size) - kernel_size//2 + ((kernel_size+1)%2)*0.5
            zz = np.arange(kernel_size) - kernel_size//2 + ((kernel_size+1)%2)*0.5
            vol_coord = np.stack(np.meshgrid(xx, yy, zz, indexing='ij'), axis=0)
            pos=torch.Tensor(vol_coord).permute(1, 2, 3, 0).reshape(-1, 3)
            edge_index = torch.stack((torch.ones(kernel_size**3) * ((kernel_size**3) //2), torch.arange(kernel_size**3)), dim=0).to(torch.long)
            # Get the spatial and spherical filter shapes
            spatial_attr, spherical_attr = invariant_attr_r3s2_fiber_bundle(pos, vec, edge_index)

            # Get unique attr
            ## Spatial
            ## To numpy then convert last dimension to tuple
            spatial_attr_np = spatial_attr.detach().cpu().numpy().round(decimals=3)
            spatial_attr_np_tuple = np.zeros((spatial_attr_np.shape[0], spatial_attr_np.shape[1]), dtype=object)
            spatial_attr_np_tuple = spatial_attr_np.view(dtype=np.dtype([('x', spatial_attr_np.dtype), ('y', spatial_attr_np.dtype)]))
            spatial_attr_np_tuple = spatial_attr_np_tuple.reshape(spatial_attr_np_tuple.shape[:-1])
            spatial_attr_unique_tuple, spatial_attr_index_tuple = np.unique(spatial_attr_np_tuple, return_inverse=True)

            ## Spherical
            spherical_attr_np = spherical_attr.detach().cpu().numpy().round(decimals=3)
            spherical_attr_unique, spherical_attr_index = np.unique(spherical_attr_np, return_inverse=True)
            #################################
            self.conv = FiberBundleConvFixedGraph(in_channels, out_channels, spatial_attr_unique_tuple.shape, spatial_attr_index_tuple,  spherical_attr_unique.shape, spherical_attr_index, kernel_size, len(vec), bias=True)
        elif conv_name == 'bekkers2':
            kernel_size = kernel_sizeSpa
            self.conv = FiberBundleConvFixedGraphMLP(in_channels, out_channels, vec, kernel_size, bias)
        else:
            raise NotImplementedError
        
    def state_dict(self, *args, **kwargs):
        """! WARNING !
        This function overrides the state dict in order to be able to save the model.
        This can be removed as soon as saving sparse matrices has been added to Pytorch.
        """
        state_dict = super().state_dict(*args, **kwargs)
        del_keys = []
        for key in state_dict:
            if key.endswith("laplacian"):
                del_keys.append(key)
        for key in del_keys:
            del state_dict[key]
        return state_dict

    def forward(self, x):
        """Forward pass.
        Args:
            x (:obj:`torch.tensor`): input [B x Fin x V x X x Y x Z]
        Returns:
            :obj:`torch.tensor`: output [B x Fout x V x X x Y x Z]
        """
        x = self.conv(x)
        return x


def precompute_projection(lap, K):
    projector = torch.zeros((K, lap.shape[0], lap.shape[0]))
    projector[0] = torch.eye(lap.shape[0])
    if K>1:
        projector[1] = lap
        for i in range(2, K):
            projector[i] = 2 * torch.mm(lap, projector[i-1]) - projector[i-2]
    return projector # K x V x V

    
##### CHEB CONV ######
class ChebConvPrecomputed(torch.nn.Module):
    """Graph convolutional layer.
    """
    def __init__(self, lap, in_channels, out_channels, kernel_size, bias=True, dense=True):
        """Initialize the Chebyshev layer.
        Args:
            in_channels (int): Number of channels/features in the input graph.
            out_channels (int): Number of channels/features in the output graph.
            kernel_size (int): Number of trainable parameters per filter, which is also the size of the convolutional kernel.
                                The order of the Chebyshev polynomials is kernel_size - 1.
            bias (bool): Whether to add a bias term.
        """
        super(ChebConvPrecomputed, self).__init__()
        projector = precompute_projection(lap.to_dense(), kernel_size).permute(0, 2, 1).reshape(kernel_size*lap.shape[0], lap.shape[0])
        self.register_buffer("projector", projector)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size

        shape = (kernel_size * in_channels, out_channels)
        self.weight = torch.nn.Parameter(torch.Tensor(*shape))
        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(1, out_channels, 1, 1, 1, 1))
        else:
            self.register_parameter("bias", None)

        self.kaiming_initialization()

    def state_dict(self, *args, **kwargs):
        """! WARNING !
        This function overrides the state dict in order to be able to save the model.
        This can be removed as soon as saving sparse matrices has been added to Pytorch.
        """
        state_dict = super().state_dict(*args, **kwargs)
        del_keys = []
        for key in state_dict:
            if key.endswith("projector"):
                del_keys.append(key)
        for key in del_keys:
            del state_dict[key]
        return state_dict

    def kaiming_initialization(self):
        """Initialize weights and bias.
        """
        std = math.sqrt(2 / (self.in_channels * self.kernel_size))
        self.weight.data.normal_(0, std)
        if self.bias is not None:
            self.bias.data.fill_(0.01)

    def forward(self, inputs):
        """Forward graph convolution.
        Args:
            laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
            inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        Returns:
            :obj:`torch.Tensor`: The convoluted inputs.
        """
        outputs = self.cheb_conv(inputs)
        if self.bias is not None:
            outputs += self.bias
        return outputs


    def cheb_conv(self, inputs):
        """Chebyshev convolution.
        Args:
            laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
            inputs (:obj:`torch.Tensor`): The current input data being forwarded.
            weight (:obj:`torch.Tensor`): The weights of the current layer.
        Returns:
            :obj:`torch.Tensor`: Inputs after applying Chebyshev convolution.
        """
        # Get tensor dimensions
        B, Fin, V, X, Y, Z = inputs.shape
        # B = batch size
        # V = nb vertices
        # Fin = nb input features
        # Fout = nb output features
        # K = order of Chebyshev polynomials + 1

        # Transform to Chebyshev basis
        inputs = inputs.permute(2, 1, 0, 3, 4, 5).reshape([V, Fin * B * X * Y * Z]) # V x Fin*B*X*Y*Z
        inputs = torch.mm(self.projector, inputs)  # K*V x Fin*B*X*Y*Z
        # Linearly compose with kernel weights
        inputs = inputs.reshape([self.kernel_size, V, Fin, B, X, Y, Z]).permute(3, 1, 4, 5, 6, 0, 2).reshape([B * V * X * Y * Z, self.kernel_size * Fin])  # B*V*X*Y*Z x K*Fin
        inputs = torch.mm(inputs, self.weight)  # B*V*X*Y*Z x Fout
        inputs = inputs.reshape([B, V, X, Y, Z, self.out_channels]).permute(0, 5, 1, 2, 3, 4)  # B x Fout x V x X x Y x Z
        
        return inputs



#### SE3 x SO3 CONV #####
class SO3SE3ConvPrecomputer(torch.nn.Module):
    """Graph convolutional layer.
    """
    def __init__(self, lap, in_channels, out_channels, kernel_sizeSph, kernel_sizeSpa, bias=True, isoSpa=True, dense=True):
        """Initialize the Chebyshev layer.
        Args:
            in_channels (int): Number of channels/features in the input graph.
            out_channels (int): Number of channels/features in the output graph.
            kernel_sizeSph (int): Number of trainable parameters per spherical filter, which is also the size of the convolutional kernel.
                                The order of the Chebyshev polynomials is kernel_size - 1.
            kernel_sizeSpa (int): Size of the spatial filter.
            bias (bool): Whether to add a bias term.
        """
        super(SO3SE3ConvPrecomputer, self).__init__()
        projector = precompute_projection(lap.to_dense(), kernel_sizeSph).permute(0, 2, 1).reshape(kernel_sizeSph*lap.shape[0], lap.shape[0])
        self.register_buffer("projector", projector)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_sizeSph = kernel_sizeSph
        self.kernel_sizeSpa = kernel_sizeSpa
        self.isoSpa = isoSpa

        shape = (out_channels, in_channels * kernel_sizeSph, 1, 1, 1)
        self.weightSph = torch.nn.Parameter(torch.Tensor(*shape))

        if self.isoSpa:
            weight_tmp, ind, distance = self.get_index(kernel_sizeSpa)
            self.register_buffer('weight_tmp', weight_tmp)
            self.ind = ind.reshape(kernel_sizeSpa, kernel_sizeSpa, kernel_sizeSpa)
            shape = (out_channels, in_channels, 1, 1, 1, self.weight_tmp.shape[-1])
        else:
            shape = (out_channels, in_channels, kernel_sizeSpa, kernel_sizeSpa, kernel_sizeSpa)
            
        self.weightSpa = torch.nn.Parameter(torch.Tensor(*shape))

        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter("bias", None)

        self.kaiming_initialization()

    def state_dict(self, *args, **kwargs):
        """! WARNING !
        This function overrides the state dict in order to be able to save the model.
        This can be removed as soon as saving sparse matrices has been added to Pytorch.
        """
        state_dict = super().state_dict(*args, **kwargs)
        del_keys = []
        for key in state_dict:
            if key.endswith("projector"):
                del_keys.append(key)
        for key in del_keys:
            del state_dict[key]
        return state_dict

    def kaiming_initialization(self):
        """Initialize weights and bias.
        """
        std = math.sqrt(2 / (self.in_channels * self.kernel_sizeSph))
        self.weightSph.data.normal_(0, std)
        std = math.sqrt(2 / (self.in_channels * (self.kernel_sizeSpa**3)))
        self.weightSpa.data.normal_(0, std)
        if self.bias is not None:
            self.bias.data.fill_(0.01)

    def get_index(self, size):
        x_mid = (size - 1)/2
        x = np.arange(size) - x_mid
        distance = np.sqrt(x[None, None, :]**2 + x[None, :, None]**2 + x[:, None, None]**2)
        unique, ind = np.unique(distance, return_inverse=True)
        weight_tmp = torch.zeros((self.out_channels, self.in_channels, size, size, size, len(unique)))
        for i in range(len(unique)):
            weight_tmp[:, :, :, :, :, i][:, :, torch.Tensor(ind.reshape((size, size, size))==i).type(torch.bool)] = 1
        return weight_tmp, ind, distance

    def forward(self, inputs):
        """Forward graph convolution.
        Args:
            laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
            inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        Returns:
            :obj:`torch.Tensor`: The convoluted inputs.
        """
        outputs = self.se3so3_conv(inputs)
        return outputs


    def se3so3_conv(self, inputs):
        """SE(3) x SO(3) grid convolution.
        Args:
            laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
            inputs (:obj:`torch.Tensor`): The current input data being forwarded.
            weightSph (:obj:`torch.Tensor`): The spherical weights of the current layer.
            weightSpa (:obj:`torch.Tensor`): The spatial weights of the current layer.
        Returns:
            :obj:`torch.Tensor`: Inputs after applying Chebyshev convolution.
        """
        # Get tensor dimensions
        B, Fin, V, X, Y, Z = inputs.shape
        
        # B = batch size
        # V = nb vertices
        # Fin = nb input features
        # Fout = nb output features
        # K = order of Chebyshev polynomials + 1

        # Transform to Chebyshev basis
        #inputs = torch.einsum('bfvxyz,kwv->bfkwxyz', inputs, self.projector).permute(0, 3, 1, 2, 4, 5, 6).reshape([B * V, Fin * self.kernel_sizeSph, X, Y, Z]) # B x Fin x K x V x X x Y x Z
        #inputs = torch.einsum('bfkvxyz,gfk->bgvxyz', inputs, self.weightSph).permute(0, 2, 1, 3, 4, 5).reshape([B * V, Fin, X, Y, Z]) # B x V x Fin x X x Y x Z

        inputs = inputs.permute(2, 1, 0, 3, 4, 5).reshape([V, Fin * B * X * Y * Z]) # V x Fin*B*X*Y*Z
        inputs = torch.mm(self.projector, inputs)  # K*V x Fin*B*X*Y*Z
        # Linearly compose with kernel weights
        inputs = inputs.reshape([self.kernel_sizeSph, V, Fin, B, X, Y, Z]).permute(3, 1, 2, 0, 4, 5, 6).reshape([B * V, Fin * self.kernel_sizeSph, X, Y, Z])  # B*V x Fin*K x X x Y x Z

        # Expand spherical and Spatial filters
        if self.kernel_sizeSpa>1:
            wSph = self.weightSph.expand(-1, -1, self.kernel_sizeSpa, self.kernel_sizeSpa, self.kernel_sizeSpa) # Fout x Fin*K x kX x kY x kZs
            if self.isoSpa:
                weightSpa = torch.sum((self.weight_tmp * self.weightSpa), -1)
            else:
                weightSpa = self.weightSpa
            wSpa = weightSpa[:, :, None].expand(-1, -1, self.kernel_sizeSph, -1, -1, -1).flatten(1, 2)
            weight = wSph * wSpa # Fout x Fin*K x kX x kY x kZ
        else:
            weight = self.weightSph
        #if self.isoSpa:
        #    weightSpa = torch.sum((self.weight_tmp * self.weightSpa), -1) # Fout x Fin x kX x kY x kZ
        #else:
        #    weightSpa = self.weightSpa # Fout x Fin x kX x kY x kZ
        
        # Convolution
        inputs = torch.nn.functional.conv3d(inputs, weight, bias=self.bias, padding='same') # B*V x Fout x X x Y x Z
        
        # Get final output tensor
        inputs = inputs.reshape([B, V, self.out_channels, X, Y, Z]).permute(0, 2, 1, 3, 4, 5).contiguous()  # B x Fout x V x X x Y x Z
        
        return inputs
    

#### SpatialConv #####
class SpatialConvPrecomputed(torch.nn.Module):
    """Graph convolutional layer.
    """
    def __init__(self, in_channels, out_channels, kernel_sizeSpa, bias=True, isoSpa=True):
        """Initialize the Chebyshev layer.
        Args:
            in_channels (int): Number of channels/features in the input graph.
            out_channels (int): Number of channels/features in the output graph.
            kernel_sizeSph (int): Number of trainable parameters per spherical filter, which is also the size of the convolutional kernel.
                                The order of the Chebyshev polynomials is kernel_size - 1.
            kernel_sizeSpa (int): Size of the spatial filter.
            bias (bool): Whether to add a bias term.
        """
        super(SpatialConvPrecomputed, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_sizeSpa = kernel_sizeSpa
        self.isoSpa = isoSpa

        if self.isoSpa:
            weight_tmp, ind, distance = self.get_index(kernel_sizeSpa)
            self.register_buffer('weight_tmp', weight_tmp)
            self.ind = ind.reshape(kernel_sizeSpa, kernel_sizeSpa, kernel_sizeSpa)
            shape = (out_channels, in_channels, 1, 1, 1, self.weight_tmp.shape[-1])
        else:
            shape = (out_channels, in_channels, kernel_sizeSpa, kernel_sizeSpa, kernel_sizeSpa)
        self.weightSpa = torch.nn.Parameter(torch.Tensor(*shape))

        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(1, out_channels, 1, 1, 1))
        else:
            self.register_parameter("bias", None)

        self.kaiming_initialization()

    def kaiming_initialization(self):
        """Initialize weights and bias.
        """
        std = math.sqrt(2 / (self.in_channels * (self.kernel_sizeSpa**3)))
        self.weightSpa.data.normal_(0, std)
        if self.bias is not None:
            self.bias.data.fill_(0.01)

    def get_index(self, size):
        x_mid = (size - 1)/2
        x = np.arange(size) - x_mid
        distance = np.sqrt(x[None, None, :]**2 + x[None, :, None]**2 + x[:, None, None]**2)
        unique, ind = np.unique(distance, return_inverse=True)
        weight_tmp = torch.zeros((self.out_channels, self.in_channels, size, size, size, len(unique)))
        for i in range(len(unique)):
            weight_tmp[:, :, :, :, :, i][:, :, torch.Tensor(ind.reshape((size, size, size))==i).type(torch.bool)] = 1
        return weight_tmp, ind, distance

    def forward(self, inputs):
        """Forward graph convolution.
        Args:
            laplacian (:obj:`torch.sparse.Tensor`): The laplacian corresponding to the current sampling of the sphere.
            inputs (:obj:`torch.Tensor`): The current input data being forwarded.
        Returns:
            :obj:`torch.Tensor`: The convoluted inputs.
        """
        redim = False
        if len(inputs.shape)==6:
            redim = True
            B, Fin, V, X, Y, Z = inputs.shape
            inputs = inputs.reshape(B, Fin * V, X, Y, Z)
        if self.isoSpa:
            weight = torch.sum((self.weight_tmp * self.weightSpa), -1)
            outputs = torch.nn.functional.conv3d(inputs, weight, padding='same') # B x Fout x X x Y x Z
        else:
            outputs = torch.nn.functional.conv3d(inputs, self.weightSpa, padding='same') # B x Fout x X x Y x Z
        
        if self.bias is not None:
            outputs += self.bias
        if redim:
            B, _, X, Y, Z = outputs.shape
            outputs = outputs.reshape(B, -1, V, X, Y, Z)
        return outputs
    




class FiberBundleConvFixedGraph(torch.nn.Module):
    """
    """
    def __init__(self, in_channels, out_channels, spatial_filter_shape, spatial_mapping, spherical_filter_shape, spherical_mapping, kernel_size, n_vec, bias=True):
        super().__init__()
        # Check arguments
        self.in_channels = in_channels
        self.out_channels = out_channels

        # Construct kernels
        self.spatial_weight = torch.nn.Parameter(torch.Tensor(*spatial_filter_shape, in_channels))
        self.spherical_weight = torch.nn.Parameter(torch.Tensor(*spherical_filter_shape, in_channels * out_channels))
        # Save filter mappings
        self.spatial_mapping = spatial_mapping
        self.spherical_mapping = spherical_mapping
        # Save filter reshaping
        self.kernel_size = kernel_size
        self.n_vec = n_vec

        # Mixing features
        self.mixing = torch.nn.Linear(in_channels, out_channels, bias=False)
        
        # Construct bias
        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(1, out_channels, 1, 1, 1, 1))
        else:
            self.register_parameter('bias', None)

        self.kaiming_initialization()

    def kaiming_initialization(self):
        """Initialize weights and bias.
        """
        std = math.sqrt(2 / (self.in_channels * self.kernel_size))
        #std = 1e-1
        self.spatial_weight.data.normal_(0, std)
        self.spherical_weight.data.normal_(0, std)
        if self.bias is not None:
            self.bias.data.fill_(0.01)
        
    def forward(self, x):
        """
        x: B, Fin, V, X, Y, Z
        """
        # Expand filters
        spatial_weight_expand = self.spatial_weight[self.spatial_mapping].reshape(self.kernel_size, self.kernel_size, self.kernel_size, self.n_vec, self.in_channels)
        spherical_weight_expand = self.spherical_weight[self.spherical_mapping].reshape((self.n_vec, self.n_vec, self.in_channels, self.out_channels)).permute(3, 2, 0, 1)

        # Do the convolutions: 1. Spatial conv, 2. Spherical conv
        B, _, _, X, Y, Z = x.shape
        x = x.reshape(B, self.in_channels*self.n_vec, X, Y, Z) # B x in_channels*n_vec x X x Y x Z
        spatial_weight_expand = spatial_weight_expand.permute(4, 3, 0, 1, 2).reshape(self.in_channels*self.n_vec, self.kernel_size, self.kernel_size, self.kernel_size).unsqueeze(1) # in_channels*n_vec x 1 x kernel_size x kernel_size x kernel_size
        x = torch.nn.functional.conv3d(x, spatial_weight_expand, bias=None, stride=1, padding='same', dilation=1, groups=self.in_channels*self.n_vec).reshape(B, self.in_channels, self.n_vec, X, Y, Z)
        x = torch.einsum('bfvxyz,gfvw->bgwxyz', x, spherical_weight_expand).contiguous()
        # Add bias
        if self.bias is not None:
            return x + self.bias
        else:  
            return x


class FiberBundleConvFixedGraphMLP(torch.nn.Module):
    """
    """
    def __init__(self, in_channels, out_channels, vec, kernel_size, bias=True):
        super().__init__()
        degree = 3
        hidden_dim = 8
        basis_dim = 8
        self.kernel_size = kernel_size
        self.in_channels = in_channels
        self.out_channels = out_channels

        xx = np.arange(kernel_size) - kernel_size//2 + ((kernel_size+1)%2)*0.5
        yy = np.arange(kernel_size) - kernel_size//2 + ((kernel_size+1)%2)*0.5
        zz = np.arange(kernel_size) - kernel_size//2 + ((kernel_size+1)%2)*0.5
        vol_coord = np.stack(np.meshgrid(xx, yy, zz, indexing='ij'), axis=0)
        pos=torch.Tensor(vol_coord).permute(1, 2, 3, 0).reshape(-1, 3)
        edge_index = torch.stack((torch.ones(kernel_size**3) * ((kernel_size**3) //2), torch.arange(kernel_size**3)), dim=0).to(torch.long)
        # Get the spatial and spherical filter shapes
        self.n_vec = vec.shape[0]
        spatial_attr, spherical_attr = invariant_attr_r3s2_fiber_bundle(pos, vec, edge_index)

        # Get unique attr
        ## Spatial
        ## To numpy then convert last dimension to tuple
        spatial_attr_np = spatial_attr.detach().cpu().numpy().round(decimals=5)
        spatial_attr_np_tuple = np.zeros((spatial_attr_np.shape[0], spatial_attr_np.shape[1]), dtype=object)
        spatial_attr_np_tuple = spatial_attr_np.view(dtype=np.dtype([('x', spatial_attr_np.dtype), ('y', spatial_attr_np.dtype)]))
        spatial_attr_np_tuple = spatial_attr_np_tuple.reshape(spatial_attr_np_tuple.shape[:-1])
        spatial_attr_unique_tuple, spatial_attr_index_tuple = np.unique(spatial_attr_np_tuple, return_inverse=True)
        spatial_attr_unique_tuple = np.array([np.array(list(x)) for x in spatial_attr_unique_tuple])
        spatial_attr_unique_tuple = torch.Tensor(spatial_attr_unique_tuple)
        self.register_buffer('spatial_attr_unique_tuple', spatial_attr_unique_tuple)
        self.spatial_attr_index_tuple = spatial_attr_index_tuple

        ## Spherical
        spherical_attr_np = spherical_attr.detach().cpu().numpy().round(decimals=3)
        spherical_attr_unique, spherical_attr_index = np.unique(spherical_attr_np, return_inverse=True)
        spherical_attr_unique = torch.Tensor(spherical_attr_unique)[..., None]
        self.register_buffer('spherical_attr_unique', spherical_attr_unique)
        self.spherical_attr_index = spherical_attr_index

        # MLP
        act_fn = torch.nn.GELU()
        self.basis = torch.nn.Sequential(PolynomialFeatures(degree), torch.nn.Linear(2*(2**degree - 1), hidden_dim), act_fn, torch.nn.Linear(hidden_dim, basis_dim), act_fn, torch.nn.Linear(basis_dim, in_channels, bias=False))
        self.fiber_basis = torch.nn.Sequential(PolynomialFeatures(degree), torch.nn.Linear(degree, hidden_dim), act_fn, torch.nn.Linear(hidden_dim, basis_dim), act_fn, torch.nn.Linear(basis_dim, in_channels * out_channels, bias=False))
        
        # Construct bias
        if bias:
            self.bias = torch.nn.Parameter(torch.Tensor(1, out_channels, 1, 1, 1, 1))
        else:
            self.register_parameter('bias', None)

        self.kaiming_initialization()

    def kaiming_initialization(self):
        """Initialize bias.
        """
        if self.bias is not None:
            self.bias.data.fill_(0.01)
        
    def forward(self, x):
        """
        x: B, Fin, V, X, Y, Z
        """
        # Expand filters
        spatial_weight_expand = self.basis(self.spatial_attr_unique_tuple)[self.spatial_attr_index_tuple].reshape(self.kernel_size, self.kernel_size, self.kernel_size, self.n_vec, self.in_channels)
        spherical_weight_expand = self.fiber_basis(self.spherical_attr_unique)[self.spherical_attr_index].reshape((self.n_vec, self.n_vec, self.in_channels, self.out_channels)).permute(3, 2, 0, 1)

        # Do the convolutions: 1. Spatial conv, 2. Spherical conv
        B, _, _, X, Y, Z = x.shape
        x = x.reshape(B, self.in_channels*self.n_vec, X, Y, Z)
        spatial_weight_expand = spatial_weight_expand.permute(4, 3, 0, 1, 2).reshape(self.in_channels*self.n_vec, self.kernel_size, self.kernel_size, self.kernel_size).unsqueeze(1)
        x = torch.nn.functional.conv3d(x, spatial_weight_expand, bias=None, stride=1, padding='same', dilation=1, groups=self.in_channels*self.n_vec).reshape(B, self.in_channels, self.n_vec, X, Y, Z)
        x = torch.einsum('bfvxyz,gfvw->bgwxyz', x, spherical_weight_expand).contiguous()
        # Add bias
        if self.bias is not None:
            return x + self.bias
        else:  
            return x
        

class PolynomialFeatures(torch.nn.Module):
    def __init__(self, degree):
        super(PolynomialFeatures, self).__init__()

        self.degree = degree

    def forward(self, x):

        polynomial_list = [x]
        for it in range(1, self.degree):
            polynomial_list.append(torch.einsum('...i,...j->...ij', polynomial_list[-1], x).flatten(-2,-1))
        return torch.cat(polynomial_list, -1)