"""Feature codecs and optional numerical conditioning for the autoencoder."""
from . import algorithms


def build_codec(input_dim, latent_dim, *, conditioning=None, codec='mlp'):
    import torch
    if codec == 'pca':
        if (any(isinstance(v, bool) or not isinstance(v, int) for v in (input_dim, latent_dim))
                or conditioning is not None or not 1 <= latent_dim < input_dim):
            raise ValueError('PCA needs a valid latent dimension and no MLP conditioning')

        class PCACodec(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.register_buffer('center', torch.zeros(input_dim))
                self.register_buffer('components', torch.zeros(latent_dim, input_dim))

            def encode(self, x):
                return (x - self.center) @ self.components.T

            def decode(self, z):
                return z @ self.components + self.center

            def forward(self, x):
                return self.decode(self.encode(x))

        return PCACodec()
    if codec != 'mlp':
        raise ValueError('Unsupported feature codec')
    if conditioning is None:
        return algorithms.build_autoencoder(input_dim, latent_dim)
    if conditioning != 'centered':
        raise ValueError('Unsupported autoencoder conditioning')

    class CenteredCodec(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.network = algorithms.build_autoencoder(input_dim, latent_dim)
            self.register_buffer('center', torch.zeros(input_dim))
            self.register_buffer('scale', torch.ones(()))

        def encode(self, x):
            return self.network.encode((x - self.center) / self.scale)

        def decode(self, z):
            return self.center + self.scale * self.network.decode(z)

        def forward(self, x):
            return self.decode(self.encode(x))

    return CenteredCodec()


def fit_pca(model, training_features):
    import torch
    if training_features.ndim != 2 or len(training_features) < 2 or not torch.isfinite(training_features).all():
        raise ValueError('PCA needs at least two finite training region features')
    with torch.no_grad():
        model.center.copy_(training_features.mean(0))
        residual = training_features - model.center
        eigenvalues, vectors = torch.linalg.eigh(residual.T @ residual / (len(residual) - 1))
        dimensions = len(model.components)
        model.components.copy_(vectors[:, -dimensions:].flip(1).T)
        return float(eigenvalues[-dimensions:].clamp_min(0).sum() / eigenvalues.clamp_min(0).sum().clamp_min(1e-12))


def initialize_conditioning(model, training_features):
    """Fit only on train; use one scalar so latent interpolation stays affine."""
    import torch
    with torch.no_grad():
        model.center.copy_(training_features.mean(0))
        model.scale.copy_((training_features - model.center).square().mean().sqrt().clamp_min(1e-6))


def codec_loss(model, prediction, target, residual_weight=0.):
    loss = algorithms.reconstruction_loss(prediction, target)
    if residual_weight:
        loss = loss + residual_weight * algorithms.reconstruction_loss(
            (prediction - model.center) / model.scale, (target - model.center) / model.scale)
    return loss
