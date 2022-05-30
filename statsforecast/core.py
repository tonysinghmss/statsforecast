# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/core.ipynb (unless otherwise specified).

__all__ = ['StatsForecast']

# Cell
import inspect
import logging
from functools import partial
from os import cpu_count

import numpy as np
import pandas as pd

# Internal Cell
logging.basicConfig(
    format='%(asctime)s %(name)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# Internal Cell
class GroupedArray:

    def __init__(self, data, indptr):
        self.data = data
        self.indptr = indptr
        self.n_groups = self.indptr.size - 1

    def __getitem__(self, idx):
        if isinstance(idx, int):
            return self.data[self.indptr[idx] : self.indptr[idx + 1]]
        elif isinstance(idx, slice):
            idx = slice(idx.start, idx.stop + 1, idx.step)
            new_indptr = self.indptr[idx].copy()
            new_data = self.data[new_indptr[0] : new_indptr[-1]].copy()
            new_indptr -= new_indptr[0]
            return GroupedArray(new_data, new_indptr)
        raise ValueError(f'idx must be either int or slice, got {type(idx)}')

    def __len__(self):
        return self.n_groups

    def __repr__(self):
        return f'GroupedArray(n_data={self.data.size:,}, n_groups={self.n_groups:,})'

    def __eq__(self, other):
        if not hasattr(other, 'data') or not hasattr(other, 'indptr'):
            return False
        return np.allclose(self.data, other.data) and np.array_equal(self.indptr, other.indptr)

    def compute_forecasts(self, h, func, xreg=None, level=None, *args):
        has_level = 'level' in inspect.signature(func).parameters and level is not None
        if has_level:
            out = np.full((h * self.n_groups, 2 * len(level) + 1), np.nan, dtype=np.float32)
            func = partial(func, level=level)
        else:
            out = np.full(h * self.n_groups, np.nan, dtype=np.float32)
        xr = None
        keys = None
        for i, grp in enumerate(self):
            if xreg is not None:
                xr = xreg[i]
            res = func(grp, h, xr, *args)
            if has_level:
                if keys is None:
                    keys = list(res.keys())
                for j, key in enumerate(keys):
                    out[h * i : h * (i + 1), j] = res[key]
            else:
                out[h * i : h * (i + 1)] = res
        return out, keys

    def compute_cv(self, h, test_size, func, input_size=None, *args):
        # output of size: (ts, window, h)
        # assuming step_size = 1 for the moment
        n_windows = test_size - h + 1
        out = np.full((self.n_groups, n_windows, h), np.nan, dtype=np.float32)
        out_test = np.full((self.n_groups, n_windows, h), np.nan, dtype=np.float32)
        for i_ts, grp in enumerate(self):
            for i_window in range(n_windows):
                cutoff = -test_size + i_window
                end_cutoff = cutoff + h
                y_train = grp[(cutoff - input_size):cutoff] if input_size is not None else grp[:cutoff]
                y_test = grp[cutoff:] if end_cutoff == 0 else grp[cutoff:end_cutoff]
                future_xreg = y_test[:, 1:] if (y_test.ndim == 2 and y_test.shape[1] > 1) else None
                out[i_ts, i_window] = func(y_train, h, future_xreg, *args)
                out_test[i_ts, i_window] = y_test[:, 0] if y_test.ndim == 2 else y_test

        return out, out_test

    def split(self, n_chunks):
        return [self[x[0] : x[-1] + 1] for x in np.array_split(range(self.n_groups), n_chunks) if x.size]

# Internal Cell
def _grouped_array_from_df(df):
    df = df.set_index('ds', append=True)
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()
    data = df.values.astype(np.float32)
    indices_sizes = df.index.get_level_values('unique_id').value_counts(sort=False)
    indices = indices_sizes.index
    sizes = indices_sizes.values
    cum_sizes = sizes.cumsum()
    dates = df.index.get_level_values('ds')[cum_sizes - 1]
    indptr = np.append(0, cum_sizes).astype(np.int32)
    return GroupedArray(data, indptr), indices, dates

# Internal Cell
def _cv_dates(last_dates, freq, h, test_size):
    #assuming step_size = 1
    n_windows = test_size - h + 1
    if len(np.unique(last_dates)) == 1:
        total_dates = pd.date_range(end=last_dates[0], periods=test_size, freq=freq)
        out = np.empty((h * n_windows, 2), dtype='datetime64[s]')
        for i_window in range(n_windows):
            out[h * i_window : h * (i_window + 1), 0] = total_dates[i_window:(i_window + h)]
            out[h * i_window : h * (i_window + 1), 1] = np.tile(total_dates[i_window] - freq * 1, h)
        dates = pd.DataFrame(np.tile(out, (len(last_dates), 1)), columns=['ds', 'cutoff'])
    else:
        dates = pd.concat([_cv_dates([ld], freq, h, test_size) for ld in last_dates])
        dates = dates.reset_index(drop=True)
    return dates

# Internal Cell
def _build_forecast_name(model, *args) -> str:
    model_name = f'{model.__name__}'
    func_params = inspect.signature(model).parameters
    func_args = list(func_params.items())[3:]  # remove input array, horizon and xreg
    changed_params = [
        f'{name}-{value}'
        for value, (name, arg) in zip(args, func_args)
        if arg.default != value
    ]
    if changed_params:
        model_name += '_' + '_'.join(changed_params)
    return model_name

# Internal Cell
def _as_tuple(x):
    if isinstance(x, tuple):
        return x
    return (x,)

# Internal Cell
def _get_n_jobs(n_groups, n_jobs, ray_address):
    if ray_address is not None:
        logger.info(
            'Using ray address,'
            'using available resources insted of `n_jobs`'
        )
        try:
            import ray
        except ModuleNotFoundError as e:
            msg = (
                '{e}. To use a ray cluster you have to install '
                'ray. Please run `pip install ray`. '
            )
            raise ModuleNotFoundError(msg) from e
        if not ray.is_initialized():
            ray.init(ray_address, ignore_reinit_error=True)
        actual_n_jobs = int(ray.available_resources()['CPU'])
    else:
        if n_jobs == -1 or (n_jobs is None):
            actual_n_jobs = cpu_count()
        else:
            actual_n_jobs = n_jobs
    return min(n_groups, actual_n_jobs)

# Cell
class StatsForecast:

    def __init__(self, df, models, freq, n_jobs=1, ray_address=None):
        self.ga, self.uids, self.last_dates = _grouped_array_from_df(df)
        self.models = models
        self.freq = pd.tseries.frequencies.to_offset(freq)
        self.n_jobs = _get_n_jobs(len(self.ga), n_jobs, ray_address)
        self.ray_address = ray_address

    def forecast(self, h, xreg=None, level=None):
        if xreg is not None:
            expected_shape = (h * len(self.ga), self.ga.data.shape[1])
            if xreg.shape != expected_shape:
                raise ValueError(f'Expected xreg to have shape {expected_shape}, but got {xreg.shape}')
            xreg, _, _ = _grouped_array_from_df(xreg)
        forecast_kwargs = dict(
            h=h, test_size=None, input_size=None,
            xreg=xreg, level=level, mode='forecast',
        )
        if self.n_jobs == 1:
            fcsts = self._sequential(**forecast_kwargs)
        else:
            fcsts = self._data_parallel(**forecast_kwargs)
        if issubclass(self.last_dates.dtype.type, np.integer):
            last_date_f = lambda x: np.arange(x + 1, x + 1 + h, dtype=self.last_dates.dtype)
        else:
            last_date_f = lambda x: pd.date_range(x + self.freq, periods=h, freq=self.freq)
        if len(np.unique(self.last_dates)) == 1:
            dates = np.tile(last_date_f(self.last_dates[0]), len(self.ga))
        else:
            dates = np.hstack([
                last_date_f(last_date)
                for last_date in self.last_dates
            ])
        idx = pd.Index(np.repeat(self.uids, h), name='unique_id')
        return pd.DataFrame({'ds': dates, **fcsts}, index=idx)

    def cross_validation(self, h, test_size, input_size=None):
        cv_kwargs = dict(
            h=h, test_size=test_size, input_size=input_size,
            xreg=None, level=None, mode='cv',
        )
        if self.n_jobs == 1:
            fcsts = self._sequential(**cv_kwargs)
        else:
            fcsts = self._data_parallel(**cv_kwargs)

        dates = _cv_dates(last_dates=self.last_dates, freq=self.freq, h=h, test_size=test_size)
        dates = {'ds': dates['ds'].values, 'cutoff': dates['cutoff'].values}
        idx = pd.Index(np.repeat(self.uids, h * (test_size - h + 1)), name='unique_id')
        return pd.DataFrame({**dates, **fcsts}, index=idx)

    def _sequential(self, h, test_size, input_size, xreg, level, mode='forecast'):
        fcsts = {}
        logger.info('Computing forecasts')
        for model_args in self.models:
            model, *args = _as_tuple(model_args)
            model_name = _build_forecast_name(model, *args)
            if mode == 'forecast':
                values, keys = self.ga.compute_forecasts(h, model, xreg, level, *args)
            elif mode == 'cv':
                values, test_values = self.ga.compute_cv(h, test_size, model, input_size, *args)
                keys = None
            if keys is not None:
                for j, key in enumerate(keys):
                    fcsts[f'{model_name}_{key}'] = values[:, j]
            else:
                fcsts[model_name] = values.flatten()
            logger.info(f'Computed forecasts for {model_name}.')
        if mode == 'cv':
            fcsts = {'y': test_values.flatten(), **fcsts}
        return fcsts

    def _data_parallel(self, h, test_size, input_size, xreg, level, mode='forecast'):
        fcsts = {}
        logger.info('Computing forecasts')
        gas = self.ga.split(self.n_jobs)
        if xreg is not None:
            xregs = xreg.split(self.n_jobs)
        else:
            from itertools import repeat

            xregs = repeat(None)

        if self.ray_address is not None:
            try:
                from ray.util.multiprocessing import Pool
            except ModuleNotFoundError as e:
                msg = (
                    f'{e}. To use a ray cluster you have to install '
                    'ray. Please run `pip install ray`. '
                )
                raise ModuleNotFoundError(msg) from e
            kwargs = dict(ray_address=self.ray_address)
        else:
            from multiprocessing import Pool
            kwargs = dict()

        with Pool(self.n_jobs, **kwargs) as executor:
            for model_args in self.models:
                model, *args = _as_tuple(model_args)
                model_name = _build_forecast_name(model, *args)
                futures = []
                for ga, xr in zip(gas, xregs):
                    if mode == 'forecast':
                        future = executor.apply_async(ga.compute_forecasts, (h, model, xr, level, *args,))
                    elif mode == 'cv':
                        future = executor.apply_async(ga.compute_cv, (h, test_size, model, input_size, *args))
                    futures.append(future)
                if mode == 'forecast':
                    values, keys = list(zip(*[f.get() for f in futures]))
                    keys = keys[0]
                elif mode == 'cv':
                    values, test_values = list(zip(*[f.get() for f in futures]))
                    keys = None
                if keys is not None:
                    values = np.vstack(values)
                    for j, key in enumerate(keys):
                        fcsts[f'{model_name}_{key}'] = values[:, j]
                else:
                    values = np.hstack([val.flatten() for val in values])
                    fcsts[model_name] = values.flatten()
                logger.info(f'Computed forecasts for {model_name}.')
        if mode == 'cv':
            test_values = np.vstack(test_values)
            fcsts = {'y': test_values.flatten(), **fcsts}
        return fcsts