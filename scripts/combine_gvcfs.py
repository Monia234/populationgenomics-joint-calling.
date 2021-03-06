#!/usr/bin/env python

"""
Combine a set of GVCFs into a MatrixTable
"""

import os
from os.path import join, basename
import subprocess
from typing import List
import logging
import shutil

import pandas as pd
import click
import hail as hl
from hail.experimental.vcf_combiner import vcf_combiner

from joint_calling.utils import get_validation_callback
from joint_calling import utils
from joint_calling import _version

logger = logging.getLogger('combine_gvcfs')
logger.setLevel('INFO')

DEFAULT_REF = 'GRCh38'
# The target number of rows per partition during each round of merging
TARGET_RECORDS = 25_000


@click.command()
@click.version_option(_version.__version__)
@click.option(
    '--meta-csv',
    'meta_csv_path',
    required=True,
    help='Sample data CSV path',
)
@click.option(
    '--out-mt',
    'out_mt_path',
    required=True,
    callback=get_validation_callback(ext='mt'),
    help='path to write the combined MatrixTable',
)
@click.option(
    '--existing-mt',
    'existing_mt_path',
    callback=get_validation_callback(ext='mt', must_exist=True),
    help='optional path to an existing MatrixTable. '
    'If provided, will be read and used as a base to get extended with the '
    'samples in the input sample map. Can be read-only, as it will not '
    'be overwritten, instead the result will be written to the new location '
    'provided with --out-mt',
)
@click.option(
    '--bucket',
    'work_bucket',
    required=True,
    help='path to folder for intermediate output. '
    'Can be a Google Storage URL (i.e. start with `gs://`).',
)
@click.option(
    '--local-tmp-dir',
    'local_tmp_dir',
    help='local directory for temporary files and Hail logs (must be local).',
)
@click.option(
    '--overwrite/--reuse',
    'overwrite',
    is_flag=True,
    help='if an intermediate or a final file exists, skip running the code '
    'that generates it.',
)
@click.option(
    '--hail-billing',
    'hail_billing',
    help='Hail billing account ID.',
)
@click.option(
    '--n-partitions',
    'n_partitions',
    type=click.INT,
    help='Number of partitions for the output matrix table',
)
def main(
    meta_csv_path: str,
    out_mt_path: str,
    existing_mt_path: str,
    work_bucket: str,
    local_tmp_dir: str,
    overwrite: bool,  # pylint: disable=unused-argument
    hail_billing: str,  # pylint: disable=unused-argument
    n_partitions: int,
):  # pylint: disable=missing-function-docstring
    local_tmp_dir = utils.init_hail('combine_gvcfs', local_tmp_dir)

    logger.info(f'Combining GVCFs')

    assert utils.file_exists(meta_csv_path)
    local_meta_csv_path = join(local_tmp_dir, basename(meta_csv_path))
    subprocess.run(
        f'gsutil cp {meta_csv_path} {local_meta_csv_path}', check=False, shell=True
    )
    new_samples_df = pd.read_table(local_meta_csv_path)

    new_mt_path = os.path.join(work_bucket, 'new.mt')
    combine_gvcfs(
        gvcf_paths=list(new_samples_df.gvcf),
        sample_names=list(new_samples_df.s),
        out_mt_path=new_mt_path,
        work_bucket=work_bucket,
        overwrite=True,
    )
    new_mt = hl.read_matrix_table(new_mt_path)
    logger.info(
        f'Written {new_mt.cols().count()} new samples to {out_mt_path}, '
        f'n_partitions={new_mt.n_partitions()}'
    )

    new_plus_existing_mt = None
    if existing_mt_path:
        logger.info(f'Combining with the existing matrix table {existing_mt_path}')
        new_plus_existing_mt = _combine_with_the_existing_mt(
            existing_mt_path=existing_mt_path,
            new_mt_path=new_mt_path,
        )

    mt = new_plus_existing_mt or new_mt
    mt.repartition(n_partitions)
    mt.write(out_mt_path, overwrite=True)
    logger.info(
        f'Written {mt.count_cols()} samples to {out_mt_path}, '
        f'n_partitions={mt.n_partitions()}'
    )

    shutil.rmtree(local_tmp_dir)


def _combine_with_the_existing_mt(
    existing_mt_path: str,
    # passing as a path because we are going
    # to re-read it with different intervals
    new_mt_path: str,
) -> hl.MatrixTable:
    # Making sure the tables are keyed by locus only, to make the combiner work.
    existing_mt = hl.read_matrix_table(existing_mt_path).key_rows_by('locus')
    intervals = vcf_combiner.calculate_new_intervals(
        existing_mt.rows(),
        n=TARGET_RECORDS,
        reference_genome=DEFAULT_REF,
    )
    new_mt = hl.read_matrix_table(new_mt_path, _intervals=intervals).key_rows_by(
        'locus'
    )
    logger.info(
        f'Combining {new_mt_path} ({new_mt.count_cols()} samples) '
        f'with an existing MatrixTable {existing_mt_path} '
        f'({existing_mt.count_cols()} samples), '
        f'split into {existing_mt.n_partitions()} partitions)'
    )
    out_mt = vcf_combiner.combine_gvcfs([existing_mt, new_mt])
    return out_mt


def combine_gvcfs(
    gvcf_paths: List[str],
    sample_names: List[str],
    out_mt_path: str,
    work_bucket: str,
    overwrite: bool = True,
):
    """
    Combine a set of GVCFs in one go
    """
    hl.experimental.run_combiner(
        gvcf_paths,
        sample_names=sample_names,
        out_file=out_mt_path,
        reference_genome=utils.DEFAULT_REF,
        use_genome_default_intervals=True,
        tmp_path=os.path.join(work_bucket, 'tmp'),
        overwrite=overwrite,
        key_by_locus_and_alleles=True,
    )


if __name__ == '__main__':
    main()  # pylint: disable=E1120
