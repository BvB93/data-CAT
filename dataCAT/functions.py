"""A module for holding functions related to the :class:`.Database` class.

Index
-----
.. currentmodule:: dataCAT.functions
.. autosummary::
    df_to_mongo_dict
    get_nan_row
    sanitize_yaml_settings
    even_index
    update_pdb_shape
    update_pdb_values
    append_pdb_values
    hdf5_availability

API
---
.. autofunction:: df_to_mongo_dict
.. autofunction:: get_nan_row
.. autofunction:: sanitize_yaml_settings
.. autofunction:: even_index
.. autofunction:: update_pdb_shape
.. autofunction:: update_pdb_values
.. autofunction:: append_pdb_values
.. autofunction:: hdf5_availability

"""

import warnings
from time import sleep
from types import MappingProxyType
from typing import (
    Collection, Union, Sequence, Tuple, List, Generator, Mapping, Any,
    Hashable, Optional, Type, TYPE_CHECKING
)

import h5py
import numpy as np
import pandas as pd

from scm.plams import Molecule, Settings
import scm.plams.interfaces.molecule.rdkit as molkit
from rdkit import Chem
from rdkit.Chem import Mol

from nanoutils import SupportsIndex, PathType
from CAT.utils import get_template

if TYPE_CHECKING:
    from .pdb_array import PDBContainer, IndexLike
    from numpy.typing import DtypeLike
else:
    PDBContainer = 'dataCAT.PDBContainer'
    IndexLike = 'dataCAT.pdb_array.IndexLike'
    DtypeLike = 'numpy.typing.DtypeLike'

__all__ = [
    'df_to_mongo_dict', 'get_nan_row', 'even_index', 'sanitize_yaml_settings',
    'update_pdb_shape', 'update_pdb_values', 'append_pdb_values', 'int_to_slice',
    'hdf5_availability'
]


def df_to_mongo_dict(df: pd.DataFrame,
                     as_gen: bool = True) -> Union[Generator, list]:
    """Convert a dataframe into a generator of dictionaries suitable for a MongoDB_ databases.

    Tuple-keys present in **df** (*i.e.* pd.MultiIndex) are expanded into nested dictionaries.

    .. _MongoDB: https://www.mongodb.com/

    Examples
    --------
    .. testsetup:: python

        >>> import pandas as pd

        >>> _columns = [('E_solv', 'Acetone'), ('E_solv', 'Acetonitrile')]
        >>> columns = pd.MultiIndex.from_tuples(_columns, names=['index', 'sub index'])

        >>> _index = [('C[O-]', 'O2'), ('CC[O-]', 'O3'), ('CCC[O-]', 'O4')]
        >>> index = pd.MultiIndex.from_tuples(_index, names=['smiles', 'anchor'])

        >>> df = pd.DataFrame([[-56.6, -57.9],
        ...                    [-56.5, -57.6],
        ...                    [-57.1, -58.2]], index=index, columns=columns)


    .. code:: python

        >>> import pandas as pd
        >>> from dataCAT.functions import df_to_mongo_dict

        >>> df = pd.DataFrame(...)  # doctest: +SKIP
        >>> print(df)  # doctest: +NORMALIZE_WHITESPACE
        index           E_solv
        sub index      Acetone Acetonitrile
        smiles  anchor
        C[O-]   O2       -56.6        -57.9
        CC[O-]  O3       -56.5        -57.6
        CCC[O-] O4       -57.1        -58.2

        >>> gen = df_to_mongo_dict(df)
        >>> print(type(gen))
        <class 'generator'>

        >>> for item in gen:
        ...     print(item)
        {'E_solv': {'Acetone': -56.6, 'Acetonitrile': -57.9}, 'smiles': 'C[O-]', 'anchor': 'O2'}
        {'E_solv': {'Acetone': -56.5, 'Acetonitrile': -57.6}, 'smiles': 'CC[O-]', 'anchor': 'O3'}
        {'E_solv': {'Acetone': -57.1, 'Acetonitrile': -58.2}, 'smiles': 'CCC[O-]', 'anchor': 'O4'}

    Parameters
    ----------
    df : :class:`pandas.DataFrame`
        A Pandas DataFrame whose axis and columns are instance of pd.MultiIndex.
    as_gen : :class:`bool`
        If :class:`True`, return a generator of dictionaries rather than a list of dictionaries.

    Returns
    -------
    :class:`Generator[dict, None, None]<typing.Generator>` or :class:`List[dict]<typing.List>`
        A generator or list of nested dictionaries construced from **df**.
        Each row in **df** is converted into a single dictionary.
        The to-be returned dictionaries are updated with a dictionary containing their respective
        (multi-)index in **df**.

    """
    def _get_dict(idx: Sequence[Hashable],
                  row: pd.Series,
                  idx_names: Sequence[Hashable]) -> dict:
        ret = {i: row[i].to_dict() for i in row.index.levels[0]}  # Add values
        ret.update(dict(zip(idx_names, idx)))  # Add index
        return ret

    if not (isinstance(df.index, pd.MultiIndex) and isinstance(df.columns, pd.MultiIndex)):
        raise TypeError("DataFrame.index and DataFrame.columns should be "
                        "instances of pandas.MultiIndex")

    idx_names = df.index.names
    if as_gen:
        return (_get_dict(idx, row, idx_names) for idx, row in df.iterrows())
    return [_get_dict(idx, row, idx_names) for idx, row in df.iterrows()]


#: A dictionary with NumPy dtypes as keys and matching :data:`None`-esque items as values.
DTYPE_DICT: Mapping[np.dtype, Any] = MappingProxyType({
    np.dtype(np.int64): -1,
    np.dtype(np.float64): np.nan,
    np.dtype(np.object_): None,
    np.dtype(np.bool_): False
})


def get_nan_row(df: pd.DataFrame) -> list:
    """Return a list of None-esque objects for each column in **df**.

    The object in question depends on the data type of the column.
    Will default to ``None`` if a specific data type is not recognized

        * :class:`~numpy.int64`: :data:`-1`
        * :class:`~numpy.float64`: :data:`~numpy.nan`
        * :class:`~numpy.object_`: :data:`None`
        * :class:`~numpy.bool_`: :data:`False`

    Parameters
    ----------
    df : :class:`pandas.DataFrame`
        A dataframe.

    Returns
    -------
    :class:`list`
        A list of none-esque objects, one for each column in **df**.

    """
    return [DTYPE_DICT.get(v.dtype, None) for _, v in df.items()]


def as_pdb_array(mol_list: Collection[Molecule], min_size: int = 0,
                 warn: bool = True) -> np.ndarray:
    """Convert a list of PLAMS molecule into an array of (partially) de-serialized .pdb files.

    Parameters
    ----------
    mol_list: :class:`Collection[Molecule]<typing.Collection>`, length :math:`m`
        A collection of :math:`m` PLAMS molecules.
    min_size : :class:`int`
        The minimumum length of the pdb_array.
        The array is padded with empty strings if required.

    Returns
    -------
    :class:`numpy.ndarray[|S80]<numpy.ndarray>`, shape :math:`(m, n)`
        An array with :math:`m` partially deserialized .pdb files with up to :math:`n` lines each.

    """
    if warn:
        msg = DeprecationWarning("'as_pdb_array()' is deprecated")
        warnings.warn(msg, stacklevel=2)

    def _get_value(mol: Molecule) -> Tuple[List[str], int]:
        """Return a partially deserialized .pdb file and the length of aforementioned file."""
        ret = Chem.MolToPDBBlock(molkit.to_rdmol(mol)).splitlines()
        return ret, len(ret)

    pdb_list, shape_list = zip(*[_get_value(mol) for mol in mol_list])

    # Construct, fill and return the pdb array
    shape = len(mol_list), max(min_size, max(shape_list))
    ret = np.zeros(shape, dtype='S80')
    for i, item in enumerate(pdb_list):
        ret[i][:len(item)] = item

    return ret


def from_pdb_array(array: np.ndarray, rdmol: bool = True,
                   warn: bool = True) -> Union[Molecule, Mol]:
    """Convert an array with a (partially) de-serialized .pdb file into a molecule.

    Parameters
    ----------
    array : :class:`numpy.ndarray[|S80]<numpy.ndarray>`, shape :math:`(n,)`
        A (partially) de-serialized .pdb file with :math:`n` lines.
    rdmol : :class:`bool`
        If :data:`bool`, return an RDKit molecule instead of a PLAMS molecule.

    Returns
    -------
    :class:`plams.Molecule<scm.plams.mol.molecule.Molecule>` or :class:`rdkit.Chem.Mol`
        A PLAMS or RDKit molecule build from **array**.

    """
    if warn:
        msg = DeprecationWarning("'from_pdb_array()' is deprecated")
        warnings.warn(msg, stacklevel=2)

    pdb_str = ''.join([item.decode() + '\n' for item in array if item])
    ret = Chem.MolFromPDBBlock(pdb_str, removeHs=False, proximityBonding=False)
    if not rdmol:
        return molkit.from_rdmol(ret)
    return ret


def sanitize_yaml_settings(settings: Settings,
                           job_type: str) -> Settings:
    """Remove a predetermined set of unwanted keys and values from a settings object.

    Parameters
    ----------
    settings : :class:`plams.Settings<scm.plams.core.settings.Settings>`
        A settings instance with, potentially, undesired keys and values.
    job_type : :class:`str`
        The name of key in the settings blacklist.

    Returns
    -------
    :class:`plams.Settings<scm.plams.core.settings.Settings>`
        A new Settings instance with all unwanted keys and values removed.

    Raises
    ------
    KeyError
        Raised if **jobtype** is not found in .../CAT/data/templates/settings_blacklist.yaml.

    """
    # Prepare a blacklist of specific keys
    blacklist = get_template('settings_blacklist.yaml')
    if job_type not in blacklist:
        return settings.copy()

    settings_del = blacklist['generic']
    settings_del.update(blacklist[job_type])

    # Recursivelly delete all keys from **s** if aforementioned keys are present in the s_del
    ret = settings.copy()
    del_nested(settings, ret, settings_del)
    return ret


def del_nested(s_ref: Settings, s_ret: dict, del_item: dict) -> None:
    """Remove all keys in **del_item** from **collection**: a (nested) dictionary and/or list."""
    empty = Settings()
    iterator = s_ref.items() if isinstance(s_ref, dict) else enumerate(s_ref)

    for key, value in iterator:
        if key in del_item:
            value_del = del_item[key]
            if isinstance(value_del, (dict, list)):
                del_nested(value, s_ret[key], value_del)  # type: ignore
            else:
                del s_ret[key]

        if value == empty:  # An empty (leftover) Settings instance: delete it
            del s_ret[key]


def even_index(df1: pd.DataFrame, df2: pd.DataFrame) -> pd.DataFrame:
    """Ensure that ``df2.index`` is a subset of ``df1.index``.

    Parameters
    ----------
    df1 : :class:`pandas.DataFrame`
        A DataFrame whose index is to-be a superset of ``df2.index``.
    df2 : :class:`pandas.DataFrame`
        A DataFrame whose index is to-be a subset of ``df1.index``.

    Returns
    -------
    :class:`pandas.DataFrame`
        A new (sorted) dataframe containing all unique elements of ``df1.index`` and ``df2.index``.
        Returns **df1** if ``df2.index`` is already a subset of ``df1.index``

    """
    # Figure out if ``df1.index`` is a subset of ``df2.index``
    bool_ar = df2.index.isin(df1.index)
    if bool_ar.all():
        return df1

    # Make ``df1.index`` a subset of ``df2.index``
    nan_row = get_nan_row(df1)
    idx = df2.index[~bool_ar]
    df_tmp = pd.DataFrame(len(idx) * [nan_row], index=idx, columns=df1.columns)
    return df1.append(df_tmp, sort=True)


def update_pdb_shape(group: h5py.Group, pdb: PDBContainer) -> None:
    """Update the shape of all datasets in **group** such that it can accommodate **pdb**.

    Parameters
    ----------
    group : :class:`h5py.Group`
        The to-be reshape h5py group.
    pdb : :class:`dataCAT.PDBContainer`
        The pdb container for updating **group**.

    """
    for name, ar in pdb.items():
        dataset = group[name]

        # Identify the new shape of all datasets
        shape = np.fromiter(dataset.shape, dtype=int)
        shape[0] += len(ar)
        if ar.ndim == 2:
            shape[1] = max(shape[1], ar.shape[1])

        # Set the new shape
        dataset.shape = shape


def update_pdb_values(group: h5py.Group, pdb: PDBContainer, idx: Optional[IndexLike]) -> None:
    """Update all datasets in **group** positioned at **index** with its counterpart from **pdb**.

    Follows the standard broadcasting rules as employed by h5py.

    Parameters
    ----------
    group : :class:`h5py.Group`
        The to-be updated h5py group.
    pdb : :class:`dataCAT.PDBContainer`
        The pdb container for updating **group**.
    idx : :class:`int`, :class:`Sequence[int]<typing.Sequence>` or :class:`slice`, optional
        An object for slicing all datasets in **group**.
        Note that, contrary to numpy, if a sequence of integers is provided
        then they'll have to ordered.

    """
    index = slice(None) if idx is None else idx

    for name, ar in pdb.items():
        dataset = group[name]

        if ar.ndim == 1:
            dataset[index] = ar  # This is actually a dataset
        else:
            _, j = ar.shape
            dataset[index, :j] = ar


def append_pdb_values(group: h5py.Group, pdb: PDBContainer) -> None:
    """Append all datasets in **group** positioned with its counterpart from **pdb**.

    Parameters
    ----------
    group : :class:`h5py.Group`
        The to-be appended h5py group.
    pdb : :class:`dataCAT.PDBContainer`
        The pdb container for appending **group**.

    """
    update_pdb_shape(group, pdb)
    for name, ar in pdb.items():
        dataset = group[name]

        if ar.ndim == 1:
            i = len(ar)
            dataset[-i:] = ar
        else:
            i, j = ar.shape
            dataset[-i:, :j] = ar


def int_to_slice(int_like: SupportsIndex, seq_len: int) -> slice:
    """Take an integer-like object and convert it into a :class:`slice`.

    The slice is constructed in such a manner that using it for slicing will
    return the same value as when passing **int_like**,
    expect that the objects dimensionanlity is larger by 1.

    Examples
    --------
    .. code:: python

        >>> import numpy as np
        >>> from dataCAT.functions import int_to_slice

        >>> array = np.ones(10)
        >>> array[0]
        1.0

        >>> idx = int_to_slice(0, len(array))
        >>> array[idx]
        array([1.])


    Parameters
    ----------
    int_like : :class:`int`
        An int-like object.
    seq_len : :class:`int`
        The length of a to-be sliced sequence.

    Returns
    -------
    :class:`slice`
        An object for slicing the sequence associated with **seq_len**.

    """
    integer = int_like.__index__()
    if integer > 0:
        if integer != seq_len:
            return slice(None, integer + 1)
        else:
            return slice(integer - 1, None)

    else:
        if integer == -1:
            return slice(integer, None)
        else:
            return slice(integer, integer + 1)


def hdf5_availability(filename: PathType, timeout: float = 5.0,
                      max_attempts: Optional[int] = 10,
                      **kwargs: Any) -> None:
    r"""Check if a .hdf5 file is opened by another process; return once it is not.

    If two processes attempt to simultaneously open a single hdf5 file then
    h5py will raise an :exc:`OSError`.

    The purpose of this method is ensure that a .hdf5 file is actually closed,
    thus allowing the :meth:`Database.from_hdf5` method to safely access **filename** without
    the risk of raising an :exc:`OSError`.

    Parameters
    ----------
    filename : :class:`str`, :class:`bytes` or :class:`os.PathLike`
        A path-like object pointing to the hdf5 file of interest.
    timeout : :class:`float`
        Time timeout, in seconds, between subsequent attempts of opening **filename**.
    max_attempts : :class:`int`, optional
        Optional: The maximum number attempts for opening **filename**.
        If the maximum number of attempts is exceeded, raise an :exc:`OSError`.
        Setting this value to :data:`None` will set the number of attempts to unlimited.
    \**kwargs : :data:`~typing.Any`
        Further keyword arguments for :class:`h5py.File`.

    Raises
    ------
    :exc:`OSError`
        Raised if **max_attempts** is exceded.

    """
    err = (f"h5py.File({filename!r}) is currently unavailable; "
           f"repeating attempt in {timeout:1.1f} seconds")

    i = max_attempts if max_attempts is not None else np.inf
    if i <= 0:
        raise ValueError(f"'max_attempts' must be larger than 0; observed value: {i!r}")

    while i:
        try:
            with h5py.File(filename, 'r+', **kwargs):
                return None  # the .hdf5 file can safely be opened
        except OSError as ex:  # the .hdf5 file cannot be safely opened yet
            warn = ResourceWarning(err)
            warn.__cause__ = exception = ex
            warnings.warn(warn)
            sleep(timeout)
        i -= 1

    raise exception


def _set_index(cls: Type[PDBContainer], group: h5py.Group,
               dtype: DtypeLike, length: int, **kwargs: Any) -> h5py.Dataset:
    scale = group.create_dataset('index', shape=(length,), maxshape=(None,), dtype=dtype, **kwargs)
    scale.make_scale('index')

    iterator = (group[k] for k in cls.keys() if k != 'index')
    for dset in iterator:
        dset.dims[0].label = 'index'
        dset.dims[0].attach_scale(scale)

    group['atoms'].dims[1].label = 'atoms'
    group['bonds'].dims[1].label = 'bonds'
    return scale
