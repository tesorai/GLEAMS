import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import scipy.sparse as ss

from gleams.feature import encoder, spectrum
from gleams.ms_io import ms_io


logger = logging.getLogger('gleams')


def _peaks_to_features(filename: str,
                       metadata: Optional[pd.DataFrame],
                       spectrum_preprocessing: Dict[str, Any],
                       enc: encoder.SpectrumEncoder)\
        -> Tuple[str, Optional[pd.DataFrame], Optional[List[ss.csr_matrix]]]:
    """
    Convert the spectra with the given identifiers in the given file to a
    feature array.

    Parameters
    ----------
    filename : str
        The peak file name.
    metadata : Optional[pd.DataFrame]
        DataFrame containing metadata for the PSMs in the peak file to be
        processed. If None, all spectra in the peak file are converted to
        features.
    spectrum_preprocessing: Dict[str, Any]
        Spectrum preprocessing settings.
    enc : encoder.SpectrumEncoder
        The SpectrumEncoder used to convert spectra to features.

    Returns
    -------
    Tuple[str, Optional[pd.DataFrame], Optional[List[ss.csr_matrix]]]
        A tuple of length 3 containing: the name of the file that has been
        converted, information about the converted spectra (scan number,
        precursor charge, and precursor m/z), the converted spectra.
        If the given file does not exist the final two elements of the tuple
        are None.
    """
    if not os.path.isfile(filename):
        logger.warning('Missing peak file %s, no features generated', filename)
        return filename, None, None
    logger.debug('Process file %s', filename)
    file_scans, file_mz, file_charge, file_encodings = [], [], [], []
    if metadata is not None:
        metadata = metadata.reset_index(['dataset', 'filename'], drop=True)
    for spec in ms_io.get_spectra(filename):
        # noinspection PyUnresolvedReferences
        if ((metadata is None or np.int64(spec.identifier) in metadata.index)
                and spectrum.preprocess(
                    spec, **spectrum_preprocessing).is_valid):
            file_scans.append(spec.identifier)
            file_mz.append(spec.precursor_mz)
            file_charge.append(spec.precursor_charge)
            file_encodings.append(enc.encode(spec))
    scans = pd.DataFrame({'scan': file_scans, 'charge': file_charge,
                          'mz': file_mz})
    scans['scan'] = scans['scan'].astype(np.int64)
    return filename, scans, file_encodings


def convert_peaks_to_features(metadata_filename: str,
                              feat_filename: str,
                              precursor_encoding: Dict[str, Any],
                              fragment_encoding: Dict[str, Any],
                              reference_encoding: Dict[str, Any],
                              filter_scans: bool = True) -> None:
    """
    Convert all peak files listed in the given metadata file to features.

    First, encoded spectra will be stored as NumPy binary files for each
    dataset. A corresponding index file for each dataset containing the peak
    filenames, spectrum identifiers, and indexes in the NumPy binary file will
    be stored as Parquet files.
    Second, all encoded spectra and index files will be concatenated into a
    single output file.

    If both a NumPy binary file and a Parquet index file for a dataset already
    exist, the corresponding dataset will _not_ be processed again.

    Parameters
    ----------
    metadata_filename : str
        The metadata file name. Should be a Parquet file.
    feat_filename : str
        The feature file name to store the encoded spectra. Should have a
        ".npz" extension.
    precursor_encoding : Dict[str, Any]
        Settings for the precursor encoder.
    fragment_encoding : Dict[str, Any]
        Settings for the fragment encoder.
    reference_encoding : Dict[str, Any]
        Settings for the reference spectrum encoder.
    filter_scans: bool
        Filter scans by the scan numbers specified in the metdata or not.
    """
    metadata = pd.read_parquet(metadata_filename)
    metadata = metadata.set_index(['dataset', 'filename', 'scan'])

    enc = encoder.MultipleEncoder([
        encoder.PrecursorEncoder(**precursor_encoding),
        encoder.FragmentEncoder(**fragment_encoding),
        encoder.ReferenceSpectraEncoder(**reference_encoding)
    ])

    logger.info('Convert peak files for metadata file %s', metadata_filename)
    feat_dir = os.path.dirname(feat_filename)
    if not os.path.isdir(feat_dir):
        try:
            os.makedirs(os.path.join(feat_dir))
        except OSError:
            pass
    dataset_total = len(metadata.index.unique('dataset'))
    for dataset_i, (dataset, metadata_dataset) in enumerate(
            metadata.groupby('dataset', as_index=False, sort=False), 1):
        # Group all encoded spectra per dataset.
        filename_encodings = os.path.join(feat_dir, f'{dataset}.npz')
        filename_index = os.path.join(feat_dir, f'{dataset}.parquet')
        if (not os.path.isfile(filename_encodings) or
                not os.path.isfile(filename_index)):
            logging.info('Process dataset %s [%3d/%3d]', dataset, dataset_i,
                         dataset_total)
            metadata_index, encodings = [], []
            for filename, file_scans, file_encodings in\
                    joblib.Parallel(n_jobs=-1, backend='multiprocessing')(
                        joblib.delayed(_peaks_to_features)
                        (fn, md_fn if filter_scans else None,
                         reference_encoding['preprocessing'], enc)
                        for fn, md_fn in metadata_dataset.groupby(
                            'filename', as_index=False, sort=False)):
                if file_scans is not None and len(file_scans) > 0:
                    metadata_index.extend([(dataset, filename, scan)
                                           for scan in file_scans['scan']])
                    encodings.extend(file_encodings)
            # Store the encoded spectra in a file per dataset.
            if len(metadata_index) > 0:
                ss.save_npz(filename_encodings, ss.vstack(encodings, 'csr'))
                metadata.loc[metadata_index].reset_index().to_parquet(
                    filename_index, index=False)
    # Combine all individual dataset features.
    _combine_features(feat_filename, feat_dir, metadata['dataset'].unique())


def _combine_features(filename: str, feat_dir: str, datasets: np.ndarray) \
        -> None:
    """
    Combine feature files for multiple datasets into a single feature file.

    If the combined feature file already exists it will _not_ be recreated.

    Parameters
    ----------
    filename : str
        The feature file name to store the encoded spectra. Should have a
        ".npz" extension.
    feat_dir : str
        Directory from which to read the encoding files for individual
        datasets.
    datasets : np.ndarray
        The datasets for which feature files will be combined.
    """
    filename_encodings = filename
    filename_index = f'{os.path.splitext(filename)[0]}.parquet'
    if os.path.isfile(filename_encodings) and os.path.isfile(filename_index):
        return
    logger.info('Combine features for %d datasets into file %s',
                len(datasets), filename_encodings)
    encodings, indexes = [], []
    for i, dataset in enumerate(datasets, 1):
        logger.debug('Append dataset %s [%3d/%3d]', dataset, i, len(datasets))
        dataset_encodings_filename = os.path.join(feat_dir, f'{dataset}.npz')
        dataset_index_filename = os.path.join(feat_dir, f'{dataset}.parquet')
        if (not os.path.isfile(dataset_encodings_filename) or
                not os.path.isfile(dataset_index_filename)):
            logger.warning('Missing features for dataset %s, skipping...',
                           dataset)
        else:
            encodings.append(ss.load_npz(dataset_encodings_filename))
            indexes.append(pq.read_table(dataset_index_filename))
    ss.save_npz(filename_encodings, ss.vstack(encodings, 'csr'))
    pq.write_table(pa.concat_tables(indexes), filename_index)
