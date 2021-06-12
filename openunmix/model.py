from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from torch.nn import Linear, Parameter, ReLU, BatchNorm3d, Conv3d, ConvTranspose3d
from .filtering import atan2
from .transforms import make_filterbanks, ComplexNorm, phasemix_sep, NSGTBase
from collections import defaultdict
import numpy as np

eps = 1.e-10


class OpenUnmix(nn.Module):
    """OpenUnmix Core spectrogram based separation module.

    Args:
        nb_bins, M (int): Number of sliCQ-NSGT tf bins
        nb_channels (int): Number of input audio channels (Default: `2`).
        input_mean (ndarray or None): global data mean of shape `(nb_bins, )`.
            Defaults to zeros(nb_bins)
        input_scale (ndarray or None): global data mean of shape `(nb_bins, )`.
            Defaults to ones(nb_bins)
    """

    def __init__(
        self,
        nb_bins,
        M,
        nb_channels=2,
        input_mean=None,
        input_scale=None,
    ):
        super(OpenUnmix, self).__init__()

        self.nb_bins = nb_bins
        self.M = M

        self.conv1 = 25
        self.conv2 = 55

        self.encoder = nn.ModuleList([
            Conv3d(nb_channels, self.conv1, (5, 11, 42), stride=(1, 1, 3)),
            ReLU(),
            BatchNorm3d(self.conv1),
            Conv3d(self.conv1, self.conv2, (5, 11, 22), stride=(1, 1, 3)),
            ReLU(),
            BatchNorm3d(self.conv2),
        ])

        self.decoder = nn.ModuleList([
            ConvTranspose3d(self.conv2, self.conv1, (5, 11, 22), stride=(1, 1, 3), output_padding=(0, 0, 2)),
            ReLU(),
            BatchNorm3d(self.conv1),
            ConvTranspose3d(self.conv1, nb_channels, (5, 11, 42), stride=(1, 1, 3), output_padding=(0, 0, 2)),
            ReLU(),
        ])

        if input_mean is not None:
            input_mean = (-input_mean).float()
        else:
            input_mean = torch.zeros(self.nb_bins)

        if input_scale is not None:
            input_scale = (1.0 / input_scale).float()
        else:
            input_scale = torch.ones(self.nb_bins)

        self.input_mean = Parameter(input_mean)
        self.input_scale = Parameter(input_scale)

    def freeze(self):
        # set all parameters as not requiring gradient, more RAM-efficient
        # at test time
        for p in self.parameters():
            p.requires_grad = False
        self.eval()

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: input spectrogram of shape
                `(nb_samples, nb_channels, nb_bins, nb_frames)`
        Returns:
            Tensor: filtered spectrogram of shape
                `(nb_samples, nb_channels, nb_bins, nb_frames)`
        """
        mix = x.detach().clone()

        nb_samples, nb_channels, nb_f_bins, nb_frames, nb_m_bins = x.shape

        x = x.reshape(nb_samples, nb_channels, nb_f_bins, -1)

        # permute so that batch is last for lstm
        x = x.permute(3, 0, 1, 2)

        # shift and scale input to mean=0 std=1 (across all bins)
        x = x + self.input_mean[: self.nb_bins]
        x = x * self.input_scale[: self.nb_bins]

        x = x.reshape(nb_samples, nb_channels, nb_frames, nb_f_bins, nb_m_bins)

        for layer in self.encoder:
            x = layer(x)

        for layer in self.decoder:
            x = layer(x)

        x = x.permute(0, 1, 3, 2, 4)

        # multiply mix by learned mask above
        #return x * mix
        return x


class Separator(nn.Module):
    """
    Separator class to encapsulate all the stereo filtering
    as a torch Module, to enable end-to-end learning.

    Args:
        targets (dict of str: nn.Module): dictionary of target models
            the spectrogram models to be used by the Separator.
        niter (int): Number of EM steps for refining initial estimates in a
            post-processing stage. Zeroed if only one target is estimated.
            defaults to `1`.
        residual (bool): adds an additional residual target, obtained by
            subtracting the other estimated targets from the mixture,
            before any potential EM post-processing.
            Defaults to `False`.
        wiener_win_len (int or None): The size of the excerpts
            (number of frames) on which to apply filtering
            independently. This means assuming time varying stereo models and
            localization of sources.
            None means not batching but using the whole signal. It comes at the
            price of a much larger memory usage.
    """

    def __init__(
        self,
        target_models: dict,
        target_models_nsgt: dict,
        sample_rate: float = 44100.0,
        nb_channels: int = 2,
        seq_dur: float = 6.0,
        seq_batch: int = 32,
        device: str = "cpu",
    ):
        super(Separator, self).__init__()

        self.nsgts = defaultdict(dict)
        self.device = device

        # separate nsgt per model
        for name, nsgt_base in target_models_nsgt.items():
            nsgt, insgt = make_filterbanks(
                nsgt_base, sample_rate=sample_rate
            )

            self.nsgts[name]['nsgt'] = nsgt
            self.nsgts[name]['insgt'] = insgt

        self.complexnorm = ComplexNorm(mono=nb_channels == 1)
        self.nb_channels = nb_channels

        # registering the targets models
        self.target_models = nn.ModuleDict(target_models)
        # adding till https://github.com/pytorch/pytorch/issues/38963
        self.nb_targets = len(self.target_models)
        self.chunk_size_samples = int(seq_dur*sample_rate)
        self.seq_batch = seq_batch
        # get the sample_rate as the sample_rate of the first model
        # (tacitly assume it's the same for all targets)
        self.register_buffer("sample_rate", torch.as_tensor(sample_rate))

    def freeze(self):
        # set all parameters as not requiring gradient, more RAM-efficient
        # at test time
        for p in self.parameters():
            p.requires_grad = False
        self.eval()

    def forward(self, audio: Tensor) -> Tensor:
        """Performing the separation on audio input

        Args:
            audio (Tensor): [shape=(nb_samples, nb_channels, nb_timesteps)]
                mixture audio waveform

        Returns:
            Tensor: stacked tensor of separated waveforms
                shape `(nb_samples, nb_targets, nb_channels, nb_timesteps)`
        """

        nb_sources = self.nb_targets
        nb_samples = audio.shape[0]
        N = audio.shape[-1]

        # getting the STFT of mix:
        # (nb_samples, nb_channels, nb_bins, nb_frames, 2)

        chunks_ceil = np.ceil(N/(self.seq_batch*self.chunk_size_samples))
        pad_last = int(chunks_ceil*self.seq_batch*self.chunk_size_samples) - N
        audio = torch.nn.functional.pad(audio, (0, pad_last))
        audio_segs = audio.reshape(-1, nb_samples*self.seq_batch, self.nb_channels, self.chunk_size_samples)

        estimates = []

        for audio_seg in audio_segs:
            seg_estimates = torch.zeros(audio_seg.shape + (nb_sources,), dtype=audio_seg.dtype, device=self.device)
            for j, (target_name, target_module) in enumerate(self.target_models.items()):
                print(f'separating {target_name}')

                nsgt = self.nsgts[target_name]['nsgt']
                insgt = self.nsgts[target_name]['insgt']

                X = nsgt(audio_seg)

                Xmag = self.complexnorm(X)

                # apply current model to get the source magnitude spectrogram
                #Xmag_segs = torch.split(Xmag, 
                Ymag = target_module(Xmag.detach().clone())

                Y = phasemix_sep(X, Ymag)
                y = insgt(Y, audio_seg.shape[-1])

                seg_estimates[..., j] = y

            estimates.append(seg_estimates)

        est_segs = torch.cat([torch.unsqueeze(est, dim=0) for est in estimates], dim=0)
        est_segs = est_segs.reshape(nb_samples, self.nb_channels, N+pad_last, -1)

        # getting to (nb_samples, nb_targets, nb_channels, nb_samples)
        ests = est_segs.permute(0, 3, 1, 2).contiguous()

        # crop the padding
        ests  = ests[..., : N]

        return ests

    def to_dict(self, estimates: Tensor, aggregate_dict: Optional[dict] = None) -> dict:
        """Convert estimates as stacked tensor to dictionary

        Args:
            estimates (Tensor): separated targets of shape
                (nb_samples, nb_targets, nb_channels, nb_timesteps)
            aggregate_dict (dict or None)

        Returns:
            (dict of str: Tensor):
        """
        estimates_dict = {}
        for k, target in enumerate(self.target_models):
            estimates_dict[target] = estimates[:, k, ...]

        if aggregate_dict is not None:
            new_estimates = {}
            for key in aggregate_dict:
                new_estimates[key] = torch.tensor(0.0)
                for target in aggregate_dict[key]:
                    new_estimates[key] = new_estimates[key] + estimates_dict[target]
            estimates_dict = new_estimates
        return estimates_dict
