#!/usr/bin/env python3
"""
Spectral line decomposition using Nested Sampling.
"""

import os
# NOTE This is a hack to avoid pecular file locking issues for the
# externally linked chunk files on the NRAO's `lustre` filesystem.
os.environ['HDF5_USE_FILE_LOCKING'] = 'FALSE'

import shutil
import warnings
import itertools
import multiprocessing
from copy import deepcopy
from pathlib import Path

import h5py
import numpy as np
import scipy as sp
import pandas as pd

import pyspeckit
import spectral_cube
from astropy import convolution
from astropy import units as u
from astropy.io import fits

from .synth_spectra import get_test_spectra
from .wrapped import (
        amm11_predict, amm22_predict,
        Prior, OrderedPrior, SpacedPrior, ResolvedWidthPrior, PriorTransformer,
        AmmoniaSpectrum, AmmoniaRunner, Dumper, run_multinest,
)


def nans(shape, dtype=None):
    return np.full(shape, np.nan, dtype=dtype)


def get_irdc_priors(size=500, vsys=0.0):
    """
    Evaluate the inverse cumulative prior functions and construct a
    `PriorTransformer` instance for use with MultiNest. These distributions are
    constructed for the IRDCs observed in Svoboda et al. (in prep).

    Parameters
    ----------
    size : int
        Number of even, linearly spaced samples in the distribution
    vsys : float
        Systemic velocity to center prior distribution about
    """
    # prior distributions
    # NOTE gamma distributions evaluate to inf at 1, so only evaluate
    # functions up to 1-epsilon. For the beta distribution ppf, 1-epsilon
    # evaluates to 0.999045 .
    epsilon = 1e-13
    x = np.linspace(0, 1-epsilon, size)
    dist_voff = sp.stats.beta(5.0, 5.0)
    dist_vdep = sp.stats.beta(1.5, 3.5)
    dist_trot = sp.stats.gamma(4.4, scale=0.070)
    dist_tex  = sp.stats.beta(1.0, 2.5)
    dist_ntot = sp.stats.beta(16.0, 14.0)
    dist_sigm = sp.stats.gamma(1.5, loc=0.03, scale=0.2)
    # interpolation values, transformed to the intervals:
    # 0 voff [-4.00,  4.0] km/s  (centered on vsys)
    #   vdep [    D,D+3.0] km/s  (with offset "D")
    # 1 trot [ 7.00, 30.0] K
    # 2 tex  [ 2.74, 12.0] K
    # 3 ntot [12.00, 17.0] log(cm^-2)
    # 4 sigm [ 0.00,  2.0] km/s
    y_voff =  8.00 * dist_voff.ppf(x) -  4.00 + vsys
    y_vdep =  3.00 * dist_vdep.ppf(x) +  0.70
    y_trot = 23.00 * dist_trot.ppf(x) +  7.00
    y_tex  =  9.26 * dist_tex.ppf(x)  +  2.74
    y_ntot =  5.00 * dist_ntot.ppf(x) + 12.00
    y_sigm =  2.00 * dist_sigm.ppf(x)
    priors = [
            #OrderedPrior(y_voff, 0),
            #SpacedPrior(Prior(y_voff, 0), Prior(y_vdep, 0)),
            #Prior(y_trot, 1),
            #Prior(y_tex,  2),
            #Prior(y_ntot, 3),
            #Prior(y_sigm, 4),
            ResolvedWidthPrior(Prior(y_voff, 0), Prior(y_sigm, 4), scale=1.5),
            Prior(y_trot, 1),
            Prior(y_tex,  2),
            Prior(y_ntot, 3),
    ]
    return PriorTransformer(priors)


def test_nested(ncomp=2, prefix='test'):
    synspec = get_test_spectra()
    spectra = [syn.to_ammspec() for syn in synspec]
    utrans = get_irdc_priors(vsys=0)
    with h5py.File('test.hdf', 'a', driver='core') as hdf:
        group = hdf.require_group(f'{prefix}/{ncomp}')
        dumper = Dumper(group, no_dump=True)
        runner = AmmoniaRunner(spectra, utrans, ncomp)
        for _ in range(20):
            run_multinest(runner, dumper, nlive=60, seed=5, tol=1.0, efr=0.3,
                    updInt=2000)
    return synspec, spectra, runner


class NoiseMap:
    def __init__(self, data):
        # NOTE The axes in the data cube are transposed, so these need to
        # be as well
        self.data = data.transpose()
        self.shape = self.data.shape

    @classmethod
    def from_pbimg(cls, rms, pb_img):
        shape = pb_img.shape
        naxes = len(shape)
        if naxes == 4:
            pb_img = pb_img[0,0]
        elif naxes == 3:
            pb_img = pb_img[0]
        elif naxes == 2:
            pass
        else:
            raise ValueError(f'Cannot parse shape : {shape}')
        # A typical primary beam image will be masked with NaNs, so replace
        # them in the noise map with Inf values.
        img = rms / pb_img
        img[~np.isfinite(img)] = np.inf
        return cls(img)

    def get_noise(self, i_lon, i_lat):
        return self.data[i_lon, i_lat]


class NoiseMapUniform:
    def __init__(self, rms):
        self.rms = rms
        self.shape = None

    def get_noise(self, i_lon, i_lat):
        return self.rms


class DataCube:
    def __init__(self, cube, noise_map):
        if isinstance(noise_map, (float, int)):
            self.noise_map = NoiseMapUniform(noise_map)
        else:
            self.noise_map = noise_map
        self._header = cube.header.copy()
        self.data, self.xarr = self.data_from_cube(cube)
        self.shape = self.data.shape
        # NOTE data is transposed so (s, b, l) -> (l, b, s)
        self.spatial_shape = (self.shape[0], self.shape[1])
        if self.noise_map.shape is not None:
            assert self.spatial_shape == self.noise_map.shape

    @property
    def full_header(self):
        return self._header

    @property
    def simple_header(self):
        # FIXME first two axes must be angular coordinates
        keys = (
                'SIMPLE', 'BITPIX',
                'NAXIS',
                'NAXIS1', 'NAXIS2',
                'WCSAXES',
                'CRPIX1', 'CRPIX2',
                'CDELT1', 'CDELT2',
                'CUNIT1', 'CUNIT2',
                'CTYPE1', 'CTYPE2',
                'CRVAL1', 'CRVAL2',
                'RADESYS',
                'EQUINOX',
        )
        hdict = {k: self._header[k] for k in keys}
        hdict['NAXIS'] = 2
        hdict['WCSAXES'] = 2
        coord_sys = ('ra', 'dec', 'lon', 'lat')
        # CTYPE's of form "RA---SIN"
        assert hdict['CTYPE1'].split('-')[0].lower() in coord_sys
        assert hdict['CTYPE2'].split('-')[0].lower() in coord_sys
        return hdict

    def data_from_cube(self, cube):
        cube = cube.to('K').with_spectral_unit('Hz')
        axis = cube.spectral_axis.value.copy()
        nu_chan = axis[1] - axis[0]
        # frequency axis needs to be ascending order
        if nu_chan < 0:
            cube = cube[::-1]
            axis = cube.spectral_axis.value.copy()
        # data is transposed such that the frequency axis is contiguous (now
        # the last or right-most in of the indices)
        data = cube._data.transpose().copy()
        return data, axis

    def get_array(self, i_lon, i_lat):
        arr = self.data[i_lon,i_lat,:]  # axes reversed from typical cube
        return arr

    def get_spectra(self, i_lon, i_lat):
        spec = self.get_array(i_lon, i_lat)
        has_nans = np.isnan(spec).any()
        noise = self.noise_map.get_noise(i_lon, i_lat)
        amm_spec = AmmoniaSpectrum(self.xarr, spec, noise)
        return amm_spec, has_nans


class CubeStack:
    def __init__(self, cubes):
        assert cubes
        self.cubes = cubes
        self.n_cubes = len(cubes)

    @property
    def full_header(self):
        return self.cubes[0].full_header

    @property
    def simple_header(self):
        return self.cubes[0].simple_header

    @property
    def shape(self):
        return self.cubes[0].shape

    @property
    def spatial_shape(self):
        return self.cubes[0].spatial_shape

    def get_arrays(self, i_lon, i_lat):
        arrays = []
        for dcube in self.cubes:
            arr = dcube.get_array(i_lon, i_lat)
            arrays.append(arr)
        return arrays

    def get_spectra(self, i_lon, i_lat):
        spectra = []
        any_nans = False
        for dcube in self.cubes:
            spec, has_nans = dcube.get_spectra(i_lon, i_lat)
            spectra.append(spec)
            any_nans |= has_nans
        return spectra, any_nans


def check_ext(store_name, ext='hdf'):
    if store_name.endswith(f'.{ext}'):
        return store_name
    else:
        return f'{store_name}.{ext}'


class HdfStore:
    linked_table = Path('table.hdf')
    chunk_prefix = 'chunk'
    dpath = '/aggregate'

    def __init__(self, store_name, nchunks=1):
        """
        Parameters
        ----------
        store_name : str
        nchunks : int
        """
        self.store_name = store_name
        self.store_dir = Path(check_ext(self.store_name, ext='store'))
        self.store_dir.mkdir(parents=True, exist_ok=True)
        # FIXME Perform error handling for if HDF file is already open
        self.hdf = h5py.File(self.store_dir / self.linked_table, 'a')
        try:
            self.nchunks = self.hdf.attrs['nchunks']
        except KeyError:
            self.hdf.attrs['nchunks'] = nchunks
            self.nchunks = nchunks

    @property
    def chunk_paths(self):
        return [
                self.store_dir / Path(f'{self.chunk_prefix}{i}.hdf')
                for i in range(self.nchunks)
        ]

    @property
    def is_open(self):
        # If the HDF file is closed, it will raise an exception stating
        # "ValueError: Not a file (not a file)"
        try:
            self.hdf.mode
            return True
        except ValueError:
            return False

    def close(self):
        self.hdf.flush()
        self.hdf.close()

    def iter_pix_groups(self):
        assert self.is_open
        for lon_pix in self.hdf['/pix']:
            if lon_pix is None:
                raise ValueError(f'Broken external HDF link: /pix/{lon_pix}')
            for lat_pix in self.hdf[f'/pix/{lon_pix}']:
                if lat_pix is None:
                    raise ValueError(f'Broken external HDF link: /pix/{lon_pix}/{lat_pix}')
                group = self.hdf[f'/pix/{lon_pix}/{lat_pix}']
                if not isinstance(group, h5py.Group):
                    continue
                yield group

    def link_files(self):
        assert self.is_open
        for chunk_path in self.chunk_paths:
            with h5py.File(chunk_path, 'r') as chunk_hdf:
                for lon_pix in chunk_hdf['/pix']:
                    for lat_pix in chunk_hdf[f'/pix/{lon_pix}']:
                        group_name = f'/pix/{lon_pix}/{lat_pix}'
                        group = h5py.ExternalLink(chunk_path.name, group_name)
                        self.hdf[group_name] = group
                self.hdf.flush()

    def reset_pix_links(self):
        assert self.is_open
        if '/pix' in self.hdf:
            del self.hdf['/pix']

    def insert_header(self, stack):
        if self.is_open:
            sh_g = self.hdf.create_group('simple_header')
            for k, v in stack.simple_header.items():
                sh_g.attrs[k] = v
            fh_g = self.hdf.create_group('full_header')
            for k, v in stack.full_header.items():
                fh_g.attrs[k] = v
            self.hdf.attrs['naxis1'] = stack.shape[0]
            self.hdf.attrs['naxis2'] = stack.shape[1]
        else:
            warnings.warn(
                    'Could not insert header: the HDF5 file is closed.',
                    category=RuntimeWarning,
            )

    def read_header(self, full=True):
        assert self.is_open
        hdr_group_name = 'full_header' if full else 'simple_header'
        h_group = self.hdf[hdr_group_name]
        header = fits.Header()
        for k, v in h_group.attrs.items():
            header[k] = v
        return header

    def create_dataset(self, dset_name, data, group='', clobber=True):
        assert len(dset_name) > 0
        self.hdf.require_group(group)
        path = f'{group.rstrip("/")}/{dset_name}'
        if path in self.hdf and clobber:
            warnings.warn(f'Deleting dataset "{path}"', RuntimeWarning)
            del self.hdf[path]
        return self.hdf[group].create_dataset(dset_name, data=data)

    def insert_fitter_pars(self, fitter):
        assert self.is_open
        self.hdf.attrs['lnZ_threshold'] = fitter.lnZ_thresh
        self.hdf.attrs['n_max_components'] = fitter.ncomp_max
        self.hdf.attrs['multinest_kwargs'] = str(fitter.mn_kwargs)


class CubeFitter:
    mn_default_kwargs = {
            'nlive':    60,
            'tol':     1.0,
            'efr':     0.3,
            'updInt': 2000,
    }

    def __init__(self, stack, utrans, lnZ_thresh=11, ncomp_max=2,
            mn_kwargs=None):
        self.stack = stack
        self.utrans = utrans
        self.lnZ_thresh = lnZ_thresh
        self.ncomp_max = ncomp_max
        self.mn_kwargs = mn_kwargs if mn_kwargs is not None else self.mn_default_kwargs

    def fit(self, *args):
        (all_lon, all_lat), chunk_path = args
        # NOTE for HDF5 files to be written correctly, they must be opened
        # *after* the `multiprocessing.Process` has been forked from the main
        # Python process, and it inherits the HDF5 libraries state.
        # See "Python and HDF5" pg. 116
        hdf = h5py.File(chunk_path, 'a')
        for (i_lon, i_lat) in zip(all_lon, all_lat):
            spectra, has_nans = self.stack.get_spectra(i_lon, i_lat)
            if has_nans:
                # FIXME replace with logging framework
                print(f'-- ({i_lon}, {i_lat}) SKIP: has NaN values')
                continue
            group_name = f'/pix/{i_lon}/{i_lat}'
            group = hdf.require_group(group_name)
            ncomp = 1
            nbest = 0
            old_lnZ = AmmoniaRunner(spectra, self.utrans, 1).null_lnZ
            assert np.isfinite(old_lnZ)
            # Iteratively fit additional components until they no longer
            # produce a significant increase in the evidence.
            while ncomp <= self.ncomp_max:
                print(f'-- ({i_lon}, {i_lat}) -> N = {ncomp}')
                sub_group = group.create_group(f'{ncomp}')
                dumper = Dumper(sub_group)
                runner = AmmoniaRunner(spectra, self.utrans, ncomp)
                # FIXME needs tuned/specific kwargs for a given ncomp
                run_multinest(runner, dumper, **self.mn_kwargs)
                assert np.isfinite(runner.run_lnZ)
                if runner.run_lnZ - old_lnZ < self.lnZ_thresh:
                    break
                else:
                    old_lnZ = runner.run_lnZ
                    nbest = ncomp
                    ncomp += 1
            group.attrs['i_lon'] = i_lon
            group.attrs['i_lat'] = i_lat
            group.attrs['nbest'] = nbest
        hdf.close()

    def fit_cube(self, store_name='run/test_cube', nproc=1):
        n_chan, n_lat, n_lon = self.stack.shape
        store = HdfStore(store_name, nchunks=nproc)
        store.insert_header(self.stack)
        store.insert_fitter_pars(self)
        # create list of indices for each process
        indices = get_multiproc_indices(self.stack.spatial_shape, store.nchunks)
        if store.nchunks == 1:
            self.fit(indices[0], store.chunk_paths[0])
        else:
            # NOTE A simple `multiprocessing.Pool` cannot be used because the
            # Cython C-extensions cannot be pickled without implementing the
            # pickling protocol on all classes.
            # NOTE `mpi4py` may be more appropriate here, but it is more complex
            # FIXME no error handling if a process fails/raises an exception
            sequence = list(zip(indices, store.chunk_paths))
            procs = [
                    multiprocessing.Process(target=self.fit, args=args)
                    for args in sequence
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join()
        # link all of the HDF5 files together
        store.link_files()
        store.close()



def get_multiproc_indices(shape, nproc):
    lon_ix, lat_ix = np.indices(shape)
    indices = [
            (lon_ix[i::nproc,...].flatten(), lat_ix[i::nproc,...].flatten())
            for i in range(nproc)
    ]
    return indices


def get_test_cubestack(full=False):
    # NOTE hack in indexing because last channel is all NaN's
    cube11 = spectral_cube.SpectralCube.read('data/test_cube_11.fits')[:-1]
    cube22 = spectral_cube.SpectralCube.read('data/test_cube_22.fits')[:-1]
    if not full:
        cube11 = cube11[:,155:195,155:195]
        cube22 = cube22[:,155:195,155:195]
    noise_map = NoiseMapUniform(rms=0.35)
    cubes = (
            DataCube(cube11, noise_map=noise_map),
            DataCube(cube22, noise_map=noise_map),
    )
    stack = CubeStack(cubes)
    return stack


def test_fit_cube(store_name='run/test_cube_multin'):
    store_filen = f'{store_name}.store'
    if Path(store_filen).exists():
        shutil.rmtree(store_filen)
    stack = get_test_cubestack(full=False)
    utrans = get_irdc_priors(vsys=63.7)  # correct for G23481 data
    fitter = CubeFitter(stack, utrans, ncomp_max=1)
    fitter.fit_cube(store_name=store_name, nproc=8)


def aggregate_store_attributes(store):
    """
    Aggregate the attribute values into a dense array from the individual
    per-pixel Nested Sampling runs. Products include:
        * 'nbest' (b, l)
        * 'evidence' (m, b, l)
        * 'evidence_err' (m, b, l)
        * 'AIC' (m, b, l)
        * 'AICc' (m, b, l)
        * 'BIC' (m, b, l)

    Parameters
    ----------
    store : HdfStore
    """
    print(':: Aggregating store attributes')
    hdf = store.hdf
    dpath = store.dpath
    n_lon = hdf.attrs['naxis1']
    n_lat = hdf.attrs['naxis2']
    ncomp_max = hdf.attrs['n_max_components']
    # dimensions (l, b, m) for evidence values
    #   (latitude, longitude, model)
    attrib_shape = (n_lon, n_lat, ncomp_max+1)
    lnz_data = nans(attrib_shape)
    lnzerr_data = nans(attrib_shape)
    bic_data = nans(attrib_shape)
    aic_data = nans(attrib_shape)
    aicc_data = nans(attrib_shape)
    # dimensions (l, b) for N-best
    nb_data = np.full((n_lon, n_lat), -1, dtype=np.int32)
    for group in store.iter_pix_groups():
        i_lon = group.attrs['i_lon']
        i_lat = group.attrs['i_lat']
        nbest = group.attrs['nbest']
        nb_data[i_lon,i_lat] = nbest
        for model in group:
            subg = group[model]
            ncomp = subg.attrs['ncomp']
            if ncomp == 1:
                lnz_data[i_lon,i_lat,0]  = subg.attrs['null_lnZ']
                bic_data[i_lon,i_lat,0]  = subg.attrs['null_BIC']
                aic_data[i_lon,i_lat,0]  = subg.attrs['null_AIC']
                aicc_data[i_lon,i_lat,0] = subg.attrs['null_AICC']
            lnz_data[i_lon,i_lat,ncomp] = subg.attrs['global_lnZ']
            lnzerr_data[i_lon,i_lat,ncomp] = subg.attrs['global_lnZ_err']
            bic_data[i_lon,i_lat,ncomp]  = subg.attrs['BIC']
            aic_data[i_lon,i_lat,ncomp]  = subg.attrs['AIC']
            aicc_data[i_lon,i_lat,ncomp] = subg.attrs['AICc']
    # transpose to dimensions (b, l)
    store.create_dataset('nbest', nb_data.transpose(), group=dpath)
    # transpose to dimensions (m, b, l)
    store.create_dataset('evidence', lnz_data.transpose(), group=dpath)
    store.create_dataset('evidence_err', lnzerr_data.tranpose(), group=dpath)
    store.create_dataset('BIC', bic_data.tranpose(), group=dpath)
    store.create_dataset('AIC', aic_data.tranpose(), group=dpath)
    store.create_dataset('AICc', aicc_data.tranpose(), group=dpath)


def convolve_evidence(store, std_pix):
    """
    Convolve the evidence maps and re-select the preferred number of model
    components. Products include:
        * 'conv_evidence' (m, b, l)
        * 'conv_nbest' (b, l)

    Parameters
    ----------
    store : HdfStore
    std_pix : number
        Standard deviation of the convolution kernel in map pixels
    """
    print(':: Convolving evidence maps')
    hdf = store.hdf
    dpath = store.dpath
    ncomp_max = hdf.attrs['n_max_components']
    lnZ_thresh = hdf.attrs['lnZ_threshold']
    data = hdf[f'{dpath}/evidence'][...]
    cdata = np.zeros_like(data)
    # Spatially convolve evidence values. The convolution operator is
    # distributive, so C(Z1-Z0) should equal C(Z1)-C(Z0).
    kernel = convolution.Gaussian2DKernel(std_pix)
    for i in range(data.shape[0]):
        cdata[i,:,:] = convolution.convolve_fft(data[i,:,:], kernel)
    # Re-compute N-best with convolved data
    nbest = np.zeros_like(cdata[0], dtype=np.int64)
    for i in range(ncomp_max):
        nbest[lnZ_thresh < cdata[i+1] - cdata[i]] = i+1
    store.create_dataset('conv_evidence', cdata, group=dpath)
    store.create_dataset('conv_nbest', nbest, group=dpath)


def aggregate_store_products(store):
    """
    Aggregate the results from the individual per-pixel Nested Sampling runs
    into dense arrays of the product values. Products include:
        * 'marg_quantiles' (M)
        * 'nbest_MAP' (m, p, b, l) -- cube of maximum a posteriori values
        * 'nbest_marginals' (m, p, M, b, l) -- marginal quantiles cube

    Parameters
    ----------
    store : HdfStore
    """
    print(':: Aggregating store products')
    hdf = store.hdf
    dpath = store.dpath
    n_lon = hdf.attrs['naxis1']
    n_lat = hdf.attrs['naxis2']
    # transpose from (b, l) -> (l, b) for consistency
    nbest_data = hdf[f'{dpath}/conv_nbest'][...].transpose()
    # get list of marginal quantile information out of store
    ncomp_max = hdf.attrs['n_max_components']
    test_group = hdf[f'pix/{n_lon//2}/{n_lat//2}/1']  # FIXME may not exist
    n_params  = test_group.attrs['n_params']
    marg_quan = test_group.attrs['marg_quantiles']
    n_margs   = len(marg_quan)
    # dimensions (l, b, p, m) for MAP-parameter values
    #   (latitude, longitude, parameter, model)
    mapdata = nans((n_lon, n_lat, n_params, ncomp_max))
    # dimensions (l, b, M, p, m) for posterior distribution marginals
    #   (latitude, longitude, marginal, parameter, model)
    # NOTE in C order, the right-most index varies the fastest
    pardata = nans((n_lon, n_lat, n_margs, n_params, ncomp_max))
    # aggregate marginals into pardata
    for group in store.iter_pix_groups():
        i_lon = group.attrs['i_lon']
        i_lat = group.attrs['i_lat']
        print(f'-- ({i_lon}, {i_lat}) aggregating values')
        nbest = nbest_data[i_lon,i_lat]
        if nbest == 0:
            continue
        nb_group = group[f'{nbest}']
        # convert MAP params from 1D array to 2D for:
        #   (p*m) -> (p, m)
        p_shape = (n_params, nbest)
        mapvs = nb_group['map_params'][...].reshape(p_shape)
        mapdata[i_lon,i_lat,:p_shape[0],:p_shape[1]] = mapvs
        # convert the marginals output 2D array to 3D for:
        #   (M, p*m) -> (M, p, m)
        m_shape = (n_margs, n_params, nbest)
        margs = nb_group['marginals'][...].reshape(m_shape)
        pardata[i_lon,i_lat,:m_shape[0],:m_shape[1],:m_shape[2]] = margs
    # transpose to dimensions (m, p, M, b, l) and then keep multi-dimensional
    # parameter cube in the HDF5 file at the native dimensions
    store.create_dataset('marg_quantiles', marg_quan, group=dpath)
    store.create_dataset('nbest_MAP', mapdata.transpose(), group=dpath)
    store.create_dataset('nbest_marginals', pardata.transpose(), group=dpath)


def aggregate_store_pdfs(store):
    """
    Aggregate the results from the individual per-pixel Nested Sampling runs
    into a dense multi-dimensional histogram of the posterior distributions.
    Products include:
        * 'pdf_bins' (p, h)
        * 'post_pdfs' (m, p, h, b, l)

    Parameters
    ----------
    store : HdfStore
    """
    print(':: Aggregating store marginal posterior PDFs')
    hdf = store.hdf
    dpath = store.dpath
    n_lon = hdf.attrs['naxis1']
    n_lat = hdf.attrs['naxis2']
    ncomp_max = hdf.attrs['n_max_components']
    nb_data = hdf[f'{dpath}/conv_nbest'][...].tranpose()
    test_group = hdf[f'pix/{n_lon//2}/{n_lat//2}/1']  # FIXME may not exist
    n_params  = test_group.attrs['n_params']
    n_bins = 200
    # dimensions (l, b, h, p, m) for histogram values
    #   (longitude, latitude, histogram-value, parameter, model)
    histdata = nans((n_lon, n_lat, n_bins-1, n_params, ncomp_max))
    # Set linear bins from limits of the posterior marginal distributions.
    # Note that 0->min and 8->max in `Dumper.quantiles` and collapse all but
    # the second axis containing the model parameters.
    margdata = hdf[f'{dpath}/nbest_marginals'][...]
    vmins = np.nanmin(margdata[:,:,0,:,:], axis=(0,2,3))
    vmaxs = np.nanmax(margdata[:,:,8,:,:], axis=(0,2,3))
    all_bins = [
            np.linspace(lo, hi, n_bins)
            for lo, hi in zip(vmins, vmaxs)
    ]
    for group in store.iter_pix_groups():
        i_lon = group.attrs['i_lon']
        i_lat = group.attrs['i_lat']
        print(f'-- ({i_lon}, {i_lat}) aggregating values')
        nbest = nb_data[i_lon,i_lat]
        if nbest == 0:
            continue
        nb_group = group[f'{nbest}']
        post = nb_group['posteriors']
        for i_par, bins in enumerate(all_bins):
            for j_par in range(ncomp_max):
                ix = i_par * ncomp_max + j_par
                hist, _ = np.histogram(
                        post[:,ix], bins=bins, density=True,
                )
                histdata[i_lon,i_lat,:,i_par,j_par] = hist
    # transpose to dimensions (m, p, h, b, l)
    store.create_dataset('pdf_bins', np.array(all_bins), group=dpath)
    store.create_dataset('post_pdfs', histdata.transpose(), group=dpath)


def convolve_post_hists(store, std_pix):
    """
    Convolve the evidence maps and re-select the preferred number of model
    components. Products include:
        * 'conv_post_pdfs' (m, p, h, b, l)

    Parameters
    ----------
    store : HdfStore
    std_pix : number
        Standard deviation of the convolution kernel in map pixels
    """
    print(':: Convolving posterior PDFs')
    hdf = store.hdf
    dpath = store.dpath
    ncomp_max = hdf.attrs['n_max_components']
    # dimensions (m, p, h, b, l)
    data = hdf[f'{dpath}/post_pdfs'][...]
    cdata = np.zeros_like(data)
    # Spatially convolve the (l, b) map for every (model, parameter,
    # histogram) set.
    kernel = convolution.Gaussian2DKernel(std_pix)
    cart_prod = itertools.product(
            range(data.shape[0]),
            range(data.shape[1]),
            range(data.shape[2]),
    )
    for i_m, i_p, i_h in cart_prod:
        cdata[i_m,i_p,i_h,:,:] = convolution.convolve_fft(
                data[i_m,i_p,i_h,:,:], kernel)
    store.create_dataset('conv_post_pdfs', cdata, group=dpath)


def aggregate_conv_pdfs(store):
    """
    Calculate weighted quantiles of convolved posterior marginal distributions.
    Products include:
        * 'conv_marginals' (m, p, h, b, l)

    Parameters
    ----------
    store : HdfStore
    """
    print(':: Calculating convolved PDF quantiles')
    hdf = store.hdf
    dpath = store.dpath
    # dimensions (p, h)
    bins = hdf[f'{dpath}/pdf_bins'][...]
    # dimensions (M)
    quan = hdf[f'{dpath}/marg_quantiles'][...]
    # dimensions (m, p, h, b, l)
    #   transposed to (m, p, b, l, h)
    data = hdf[f'{dpath}/conv_post_pdfs'][...]
    data = data.tranpose((0, 1, 3, 4, 2))
    data = np.cumsum(data, axis=4) / np.sum(data, axis=4)
    # dimensions (m, p, b, l, M)
    # TODO tranpose to more efficient axes ordering
    margs_shape = list(data.shape)
    margs_shape[-1] = len(quan)
    margs = np.empty(margs_shape)
    cart_prod = itertools.product(
            range(data.shape[0]),
            range(data.shape[2]),
            range(data.shape[3]),
    )
    for i_p, x in enumerate(bins):
        for i_m, i_b, i_l in cart_prod:
            y = data[i_m,i_p,i_b,i_l]
            margs[i_m,i_p,i_b,i_l,:] = np.interp(quan, y, x)
    # tranpose back to conventional shape (m, p, h, b, l)
    margs = margs.transpose((0, 1, 4, 2, 3))
    store.create_dataset('conv_marginals', margs, group=dpath)


def postprocess_run(store, std_pix=None):
    aggregate_store_attributes(store)
    convolve_evidence(store, std_pix)
    aggregate_store_products(store)
    aggregate_store_pdfs(store)
    convolve_post_pdfs(store, std_pix)
    aggregate_conv_pdfs(store)


def test_pyspeckit_profiling_compare(n=100):
    # factors which provide constant overhead
    s11, s22 = get_test_spectra()
    xarr = s11.xarr.value.copy()
    data = s11.sampled_spec
    params = np.array([-1.0, 10.0, 4.0, 14.5,  0.3])
    #        ^~~~~~~~~ voff, trot, tex, ntot, sigm
    amms = AmmoniaSpectrum(xarr, data, 0.1)
    # loop spectra to average function calls by themselves
    for _ in range(n):
        pyspeckit.spectrum.models.ammonia.ammonia(
                s11.xarr, xoff_v=-1.0, trot=10.0, tex=4.0, ntot=14.5,
                width=0.3, fortho=0, line_names=['oneone'])
        amm11_predict(amms, params)


