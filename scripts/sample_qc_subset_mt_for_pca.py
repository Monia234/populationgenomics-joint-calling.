#!/usr/bin/env python

"""
Combine with HGDP-1KG subset of gnomAD v3, and select high-quality QC sites for PCA.

This script will produce two matrix tables:
1. `--out-hgdp-union-mt` - a union of all input dataset and HGDP-1KG samples,
   with rows corresponding to the rows shared with datasets, and fitered to
   high-quality ld-pruned sites best suited for PCA. The table is stipped off of
   all sample and entry annotations but GT, and with the HGDP-1KG sample annotations
   re-added as a `hgdp_1kg_metadata` column.
2. `--out-mt` - the input dataset with rows filtered down to the rows in 
   `--out-hgdp-union-mt` (so only high-quality sites shared with HGDP-1KG).
"""

import logging
from typing import Optional

import click
import hail as hl
from hail.experimental import lgt_to_gt

from joint_calling import utils
from joint_calling import _version


logger = logging.getLogger(__file__)
logging.basicConfig(format='%(levelname)s (%(name)s %(lineno)s): %(message)s')
logger.setLevel(logging.INFO)


@click.command()
@click.version_option(_version.__version__)
@click.option(
    '--mt',
    'mt_path',
    required=True,
    callback=utils.get_validation_callback(ext='mt', must_exist=True),
    help='path to the Matrix Table',
)
@click.option(
    '--out-hgdp-union-mt',
    'out_hgdp_union_mt_path',
    required=True,
    callback=utils.get_validation_callback(ext='mt'),
    help='path to write the combined Matrix Table with HGDP-1KG. The difference with `--out-mt` is that it also contains HGDP-1KG samples',
)
@click.option(
    '--out-mt',
    'out_mt_path',
    required=True,
    callback=utils.get_validation_callback(ext='mt'),
    help='path to write the Matrix Table after subsetting to selected rows. The difference with --out-hgdp-union-mt is that it contains only the dataset samples',
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
    required=True,
    help='Hail billing account ID.',
)
def main(  # pylint: disable=too-many-arguments,too-many-locals,missing-function-docstring
    mt_path: str,
    out_mt_path: str,
    out_hgdp_union_mt_path: str,
    overwrite: bool,  # pylint: disable=unused-argument
    hail_billing: str,  # pylint: disable=unused-argument
):
    utils.init_hail('sample_qc_subset_mt_for_pca')

    mt = utils.get_mt(mt_path, passing_sites_only=True)
    # Subset to biallelic SNPs in autosomes
    mt = mt.filter_rows(
        (hl.len(mt.alleles) == 2)
        & hl.is_snp(mt.alleles[0], mt.alleles[1])
        & (mt.locus.in_autosome())
    )
    mt = mt.key_rows_by('locus', 'alleles')
    mt = hl.experimental.densify(mt)

    hgdp_union_mt = get_sites_shared_with_hgdp(
        mt=mt,
        hgdp_mt=hl.read_matrix_table(utils.GNOMAD_HGDP_1KG_MT_PATH),
    )

    hgdp_union_hq_sites_mt = filter_high_quality_sites(
        hgdp_union_mt,
        out_mt_path=out_hgdp_union_mt_path,
    )
    generate_subset_mt(
        mt=mt,
        hgdp_union_hq_sites_mt=hgdp_union_hq_sites_mt,
        out_mt_path=out_mt_path,
    )


def get_sites_shared_with_hgdp(
    mt: hl.MatrixTable,
    hgdp_mt: hl.MatrixTable,
    out_mt_path: Optional[str] = None,
) -> hl.MatrixTable:
    """
    1. Strip off column- and entry-level annotations
    2. Combine the dataset with HGDP/1kG (`hgdp_mt`) and keep only shared rows
    3. Add back HGDP column annotations as a `hgdp_1kg_metadata` column
    """

    # The hgdp/1kg table is already dense and annotated with GT,
    # so just need to make sure the analysed dataset table matches that:
    mt = mt.select_entries(GT=lgt_to_gt(mt.LGT, mt.LA))

    # Entries and columns must be identical, so stripping all column-level data,
    # and all entry-level data except for GT. But first saving the columns
    # (i.e. sample-level metadata) into a variable to re-add it later:
    hgdp_cols_ht = hgdp_mt.cols()
    hgdp_mt = hgdp_mt.select_entries(hgdp_mt.GT).select_cols()

    # Join samples between two datasets. It will also subset rows to the rows
    # shared between datasets.
    mt = hgdp_mt.union_cols(mt)

    # Add in back the sample-level metadata
    mt = mt.annotate_cols(hgdp_1kg_metadata=hgdp_cols_ht[mt.s])

    if out_mt_path:
        mt.write(out_mt_path, overwrite=True)
        mt = mt.read_matrix_table(out_mt_path)
    return mt


def filter_high_quality_sites(
    mt: hl.MatrixTable,
    num_rows_before_ld_prune: int = 200_000,
    out_mt_path: Optional[str] = None,
) -> hl.MatrixTable:
    """
    1. Run `hl.variant_qc()` to calculate metrics such as AF, call rate
        and inbereeding coefficient
    2. Select variants based off of gnomAD v3 criteria: AF > 1%, call rate > 99%,
        inbreeding coefficient >-0.25 (no excess of heterozygotes)
    3. Randomly subset sites to `num_rows_before_ld_prune`
    4. LD-prune
    """

    # Choose variants based off of gnomAD v3 criteria
    mt = hl.variant_qc(mt)
    mt = mt.annotate_rows(IB=hl.agg.inbreeding(mt.GT, mt.variant_qc.AF[1]))
    mt = mt.filter_rows(
        (mt.variant_qc.AF[1] > 0.01)
        & (mt.variant_qc.call_rate > 0.99)
        & (mt.IB.f_stat > -0.25)
    )

    # Randomly subsampling the matrix table to `num_rows_before_ld_prune` sites
    # before feeding it into LD prunning
    mt = mt.cache()
    nrows = mt.count_rows()
    logger.info(f'Number of rows before subsetting: {nrows}')
    mt = mt.sample_rows(num_rows_before_ld_prune / nrows, seed=12345)

    # LD prunning
    pruned_variant_ht = hl.ld_prune(mt.GT, r2=0.1, bp_window_size=500000)
    mt = mt.filter_rows(hl.is_defined(pruned_variant_ht[mt.row_key]))
    logger.info(f'Number of rows after prunning: {mt.count_rows()}')

    if out_mt_path:
        mt.write(out_mt_path, overwrite=True)
        mt = mt.read_matrix_table(out_mt_path)
    return mt


def generate_subset_mt(
    mt: hl.MatrixTable,
    hgdp_union_hq_sites_mt: hl.MatrixTable,
    out_mt_path: Optional[str] = None,
) -> hl.MatrixTable:
    """
    Subset the matrix table `mt` down to sites in `hgdp_union_hq_sites_mt`
    """

    # Filter `mt` down to the loci in `hgdp_union_hq_sites_mt`
    mt = mt.semi_join_rows(hgdp_union_hq_sites_mt.rows())
    mt = mt.cache()
    logger.info(f'Number of rows: {mt.count_rows()}')
    mt = mt.repartition(1000, shuffle=False)

    if out_mt_path:
        mt.write(out_mt_path, overwrite=True)
        mt = mt.read_matrix_table(out_mt_path)
    return mt


if __name__ == '__main__':
    main()  # pylint: disable=E1120