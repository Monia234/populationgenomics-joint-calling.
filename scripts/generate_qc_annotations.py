#!/usr/bin/env python

"""
Generates input for random_forest.py
- info-split.ht
"""

import logging
from os.path import join, basename
from typing import Dict, Optional
import subprocess
import click
import hail as hl

from gnomad.sample_qc.relatedness import generate_trio_stats_expr
from gnomad.utils.annotations import (
    add_variant_type,
    annotate_adj,
    get_adj_expr,
    get_lowqual_expr,
)
from gnomad.utils.filtering import filter_to_autosomes
from gnomad.utils.sparse_mt import (
    get_as_info_expr,
    get_site_info_expr,
    INFO_INT32_SUM_AGG_FIELDS,
    INFO_SUM_AGG_FIELDS,
    split_info_annotation,
    split_lowqual_annotation,
)
from gnomad.utils.vcf import ht_to_vcf_mt
from gnomad.utils.vep import vep_or_lookup_vep

from joint_calling.utils import file_exists
from joint_calling import utils, _version
import joint_calling

logger = logging.getLogger('qc-annotations')
logger.setLevel(logging.INFO)


@click.command()
@click.version_option(_version.__version__)
@click.option(
    '--out-info-ht',
    'out_info_ht_path',
    required=True,
)
@click.option(
    '--out-info-vcf',
    'out_info_vcf_path',
)
@click.option(
    '--out-allele-data-ht',
    'out_allele_data_ht_path',
    required=True,
)
@click.option(
    '--out-qc-ac-ht',
    'out_qc_ac_ht_path',
    required=True,
)
@click.option(
    '--out-vep-ht',
    'out_vep_ht_path',
)
@click.option(
    '--mt',
    'mt_path',
    required=True,
    callback=utils.get_validation_callback(ext='mt', must_exist=True),
    help='path to the input MatrixTable',
)
@click.option(
    '--hard-filtered-samples-ht',
    'hard_filtered_samples_ht',
    required=True,
    help='Path to a table with only samples that passed filters '
    '(it\'s generated by sample QC)',
)
@click.option(
    '--meta-ht',
    'meta_ht',
    required=True,
    help='',
)
@click.option(
    '--bucket',
    'work_bucket',
    required=True,
    help='path to write intermediate output and checkpoints. '
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
    '--split-multiallelic',
    'split_multiallelic',
    is_flag=True,
)
@click.option(
    '--vep-version',
    'vep_version',
)
@click.option(
    '--fam-file',
    'trios_fam_ped_file',
)
def main(  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements,missing-function-docstring
    out_info_ht_path: str,
    out_info_vcf_path: str,
    out_allele_data_ht_path: str,
    out_qc_ac_ht_path: str,
    out_vep_ht_path: str,
    mt_path: str,
    hard_filtered_samples_ht: str,
    meta_ht: str,
    work_bucket: str,
    local_tmp_dir: str,
    overwrite: bool,
    split_multiallelic: bool,
    vep_version: Optional[str],
    trios_fam_ped_file: Optional[str],
):
    local_tmp_dir = utils.init_hail('qc_annotations', local_tmp_dir)

    all_samples_mt = utils.get_mt(mt_path)
    hard_filtered_mt = utils.get_mt(
        mt_path,
        hard_filtered_samples_to_remove_ht=hl.read_table(hard_filtered_samples_ht),
        meta_ht=hl.read_table(meta_ht),
        add_meta=True,
    )

    compute_info(
        out_ht_path=out_info_ht_path,
        out_vcf_path=out_info_vcf_path,
        mt=all_samples_mt,
        overwrite=overwrite,
        split_multiallelic=split_multiallelic,
    )

    generate_allele_data(
        out_ht_path=out_allele_data_ht_path,
        ht=hard_filtered_mt.rows(),
        overwrite=overwrite,
    )

    # TODO: compute AC and qc_AC as part of compute_info
    qc_ac_ht = generate_ac(
        out_ht_path=out_qc_ac_ht_path,
        mt=hard_filtered_mt,
        overwrite=overwrite,
    )

    # generate_fam_stats
    fam_stats_ht = None
    if trios_fam_ped_file:
        fam_stats_ht = generate_fam_stats(
            hard_filtered_mt,
            work_bucket=work_bucket,
            overwrite=overwrite,
            trios_fam_ped_file=trios_fam_ped_file,
        )

    if fam_stats_ht:
        export_transmitted_singletons_vcf(
            fam_stats_ht=fam_stats_ht,
            qc_ac_ht=qc_ac_ht,
            work_bucket=work_bucket,
            overwrite=overwrite,
        )

    if vep_version or out_vep_ht_path:
        if not out_vep_ht_path:
            logger.critical('--out-vep-ht must be specified along with --vep-version')
        run_vep(
            out_ht_path=out_vep_ht_path,
            mt=all_samples_mt,
            vep_version=vep_version,
            work_bucket=work_bucket,
            overwrite=overwrite,
        )


def compute_info(
    out_ht_path: str,
    out_vcf_path: str,
    mt: hl.MatrixTable,
    overwrite: bool = False,
    split_multiallelic: bool = False,
) -> hl.Table:
    """
    Computes a HT with the typical GATK AS and site-level info fields
    as well as ACs and lowqual fields.
    Note that this table doesn't split multi-allelic sites.
    :param mt: MatrixTable, keyed by locus and allele, soft-filtered
    :param work_bucket: bucket path to write checkpoints
    :param overwrite: overwrite checkpoints if they exist
    :param split_multiallelic: split multiallelic variants in the info table
    :param export_info_vcf: genereate a VCF from the into table
    :return: Table with info fields
    """
    logger.info('Compute info')
    if not overwrite and file_exists(out_ht_path):
        info_ht = hl.read_table(out_ht_path)
    else:
        mt = mt.filter_rows((hl.len(mt.alleles) > 1))
        mt = mt.transmute_entries(**mt.gvcf_info)
        mt = mt.annotate_rows(alt_alleles_range_array=hl.range(1, hl.len(mt.alleles)))

        # Compute AS and site level info expr
        # Note that production defaults have changed:
        # For new releases, the `RAW_MQandDP` field replaces the `RAW_MQ` and `MQ_DP` fields
        info_expr = get_site_info_expr(
            mt,
            sum_agg_fields=INFO_SUM_AGG_FIELDS,
            int32_sum_agg_fields=INFO_INT32_SUM_AGG_FIELDS,
            array_sum_agg_fields=['SB', 'RAW_MQandDP'],
        )
        info_expr = info_expr.annotate(
            **get_as_info_expr(
                mt,
                sum_agg_fields=INFO_SUM_AGG_FIELDS,
                int32_sum_agg_fields=INFO_INT32_SUM_AGG_FIELDS,
                array_sum_agg_fields=['SB', 'RAW_MQandDP'],
            )
        )

        # Add AC and AC_raw:
        # First compute ACs for each non-ref allele, grouped by adj
        grp_ac_expr = hl.agg.array_agg(
            lambda ai: hl.agg.filter(
                mt.LA.contains(ai),
                hl.agg.group_by(
                    get_adj_expr(mt.LGT, mt.GQ, mt.DP, mt.LAD),
                    hl.agg.sum(
                        mt.LGT.one_hot_alleles(mt.LA.map(hl.str))[mt.LA.index(ai)]
                    ),
                ),
            ),
            mt.alt_alleles_range_array,
        )

        # Then, for each non-ref allele, compute
        # AC as the adj group
        # AC_raw as the sum of adj and non-adj groups
        info_expr = info_expr.annotate(
            AC_raw=grp_ac_expr.map(
                lambda i: hl.int32(i.get(True, 0) + i.get(False, 0))
            ),
            AC=grp_ac_expr.map(lambda i: hl.int32(i.get(True, 0))),
        )

        # Annotating raw MT with pab max
        info_expr = info_expr.annotate(
            AS_pab_max=hl.agg.array_agg(
                lambda ai: hl.agg.filter(
                    mt.LA.contains(ai) & mt.LGT.is_het(),
                    hl.agg.max(
                        hl.binom_test(
                            mt.LAD[mt.LA.index(ai)], hl.sum(mt.LAD), 0.5, 'two-sided'
                        )
                    ),
                ),
                mt.alt_alleles_range_array,
            )
        )

        info_ht = mt.select_rows(info=info_expr).rows()

        # Add lowqual flag
        info_ht = info_ht.annotate(
            lowqual=get_lowqual_expr(
                info_ht.alleles,
                info_ht.info.QUALapprox,
                # The indel het prior used for gnomad v3 was 1/10k bases (phred=40).
                # This value is usually 1/8k bases (phred=39).
                indel_phred_het_prior=40,
            ),
            AS_lowqual=get_lowqual_expr(
                info_ht.alleles, info_ht.info.AS_QUALapprox, indel_phred_het_prior=40
            ),
        )

        info_ht = info_ht.naive_coalesce(7500)

        if split_multiallelic:
            info_ht = split_multiallelic_in_info_table(info_ht)

        info_ht.write(out_ht_path, overwrite=True)

    if out_vcf_path and (not file_exists(out_vcf_path) or overwrite):
        hl.export_vcf(ht_to_vcf_mt(info_ht), out_vcf_path)

    return info_ht


def split_multiallelic_in_info_table(info_ht: hl.Table) -> hl.Table:
    """
    Generates an info table that splits multi-allelic sites from the multi-allelic
    info table.
    :return: Info table with split multi-allelics
    :rtype: Table
    """

    # Create split version
    info_ht = hl.split_multi(info_ht)

    info_ht = info_ht.annotate(
        info=info_ht.info.annotate(
            **split_info_annotation(info_ht.info, info_ht.a_index),
        ),
        AS_lowqual=split_lowqual_annotation(info_ht.AS_lowqual, info_ht.a_index),
    )
    return info_ht


def generate_allele_data(
    out_ht_path: str,
    ht: hl.Table,
    overwrite: bool,
) -> hl.Table:
    """
    Returns bi-allelic sites HT with the following annotations:
     - allele_data (nonsplit_alleles, has_star, variant_type, and n_alt_alleles)
    :param Table ht: Full unsplit HT
    :return: Table with allele data annotations
    :param overwrite: overwrite checkpoints if they exist
    :rtype: Table
    """
    logger.info('Generate allele data')
    if not overwrite and file_exists(out_ht_path):
        return hl.read_table(out_ht_path)

    ht = ht.select()
    allele_data = hl.struct(
        nonsplit_alleles=ht.alleles, has_star=hl.any(lambda a: a == '*', ht.alleles)
    )
    ht = ht.annotate(allele_data=allele_data.annotate(**add_variant_type(ht.alleles)))

    ht = hl.split_multi_hts(ht)
    ht = ht.filter(hl.len(ht.alleles) > 1)
    allele_type = (
        hl.case()
        .when(hl.is_snp(ht.alleles[0], ht.alleles[1]), 'snv')
        .when(hl.is_insertion(ht.alleles[0], ht.alleles[1]), 'ins')
        .when(hl.is_deletion(ht.alleles[0], ht.alleles[1]), 'del')
        .default('complex')
    )
    ht = ht.annotate(
        allele_data=ht.allele_data.annotate(
            allele_type=allele_type, was_mixed=ht.allele_data.variant_type == 'mixed'
        )
    )
    ht.write(out_ht_path, overwrite=True)
    return ht


def generate_ac(out_ht_path: str, mt: hl.MatrixTable, overwrite: bool) -> hl.Table:
    """
    Creates Table containing allele counts per variant.
    Returns table containing the following annotations:
        - `ac_qc_samples_raw`: Allele count of high quality samples
        - `ac_qc_samples_unrelated_raw`: Allele count of high quality
           unrelated samples
        - `ac_release_samples_raw`: Allele count of release samples
        - `ac_qc_samples_adj`: Allele count of high quality samples after
           adj filtering
        - `ac_qc_samples_unrelated_adj`: Allele count of high quality
           unrelated samples after adj filtering
        - `ac_release_samples_adj`: Allele count of release samples after
           adj filtering
    :param mt: Input MatrixTable
    :return: Table containing allele counts
    """
    logger.info('Generate AC per variant')
    if not overwrite and file_exists(out_ht_path):
        logger.info(f'Reusing {out_ht_path}')
        return hl.read_table(out_ht_path)

    mt = hl.experimental.sparse_split_multi(mt, filter_changed_loci=True)
    mt = mt.filter_cols(mt.meta.high_quality)
    mt = mt.filter_rows(hl.len(mt.alleles) > 1)
    mt = annotate_adj(mt)
    mt = mt.annotate_rows(
        ac_qc_samples_raw=hl.agg.sum(mt.GT.n_alt_alleles()),
        ac_qc_samples_unrelated_raw=hl.agg.filter(
            ~mt.meta.all_samples_related,
            hl.agg.sum(mt.GT.n_alt_alleles()),
        ),
        ac_release_samples_raw=hl.agg.filter(
            mt.meta.release, hl.agg.sum(mt.GT.n_alt_alleles())
        ),
        ac_qc_samples_adj=hl.agg.filter(mt.adj, hl.agg.sum(mt.GT.n_alt_alleles())),
        ac_qc_samples_unrelated_adj=hl.agg.filter(
            ~mt.meta.all_samples_related & mt.adj,
            hl.agg.sum(mt.GT.n_alt_alleles()),
        ),
        ac_release_samples_adj=hl.agg.filter(
            mt.meta.release & mt.adj, hl.agg.sum(mt.GT.n_alt_alleles())
        ),
    )
    ht = mt.rows()
    ht = ht.repartition(10000, shuffle=False)
    ht.write(out_ht_path, overwrite=True)
    return ht


def generate_fam_stats(
    mt: hl.MatrixTable, work_bucket: str, overwrite: bool, trios_fam_ped_file: str
) -> hl.Table:
    """
    Calculate transmission and de novo mutation statistics using trios in the dataset.
    :param mt: Input MatrixTable
    :param fam_file: path to text file containing trio pedigree
    :return: Table containing trio stats
    """
    logger.info('Generate FAM stats')
    out_ht_path = join(work_bucket, 'fam_stats.ht')
    if not overwrite and file_exists(out_ht_path):
        return hl.read_table(out_ht_path)

    mt = hl.experimental.sparse_split_multi(mt, filter_changed_loci=True)

    # Load Pedigree data and filter MT to samples present in any of the trios
    ped = hl.Pedigree.read(trios_fam_ped_file, delimiter='\t')
    fam_ht = hl.import_fam(trios_fam_ped_file, delimiter='\t')
    fam_ht = fam_ht.annotate(fam_members=[fam_ht.id, fam_ht.pat_id, fam_ht.mat_id])
    fam_ht = fam_ht.explode('fam_members', name='s')
    fam_ht = fam_ht.key_by('s').select().distinct()

    mt = mt.filter_cols(hl.is_defined(fam_ht[mt.col_key]))
    logger.info(
        f'Generating family stats using {mt.count_cols()} samples from {len(ped.trios)} trios.'
    )

    mt = filter_to_autosomes(mt)
    mt = annotate_adj(mt)
    mt = mt.select_entries('GT', 'GQ', 'AD', 'END', 'adj')
    mt = hl.experimental.densify(mt)
    mt = mt.filter_rows(hl.len(mt.alleles) == 2)
    mt = hl.trio_matrix(mt, pedigree=ped, complete_trios=True)
    trio_adj = mt.proband_entry.adj & mt.father_entry.adj & mt.mother_entry.adj

    ht = mt.select_rows(
        **generate_trio_stats_expr(
            mt,
            transmitted_strata={'raw': True, 'adj': trio_adj},
            de_novo_strata={
                'raw': True,
                'adj': trio_adj,
            },
            proband_is_female_expr=mt.is_female,
        )
    ).rows()

    ht = ht.filter(
        ht.n_de_novos_raw + ht.n_transmitted_raw + ht.n_untransmitted_raw > 0
    )
    ht = ht.repartition(10000, shuffle=False)
    ht.write(out_ht_path, overwrite=True)
    return ht


def export_transmitted_singletons_vcf(
    fam_stats_ht: hl.Table,
    qc_ac_ht: hl.Table,
    work_bucket: str,
    overwrite: bool = False,
) -> Dict[str, str]:
    """
    Exports the transmitted singleton Table to a VCF.
    :return: None
    """
    output_vcf_paths = {
        conf: join(work_bucket, 'transmitted-singletons-{conf}.vcf.bgz')
        for conf in ['adj', 'raw']
    }
    if not overwrite and all(file_exists(path) for path in output_vcf_paths.values()):
        return output_vcf_paths

    for transmission_confidence in ['raw', 'adj']:
        ts_ht = qc_ac_ht.filter(
            (
                fam_stats_ht[qc_ac_ht.key][f'n_transmitted_{transmission_confidence}']
                == 1
            )
            & (qc_ac_ht.ac_qc_samples_raw == 2)
        )
        ts_ht = ts_ht.annotate(s=hl.null(hl.tstr))
        ts_mt = ts_ht.to_matrix_table_row_major(columns=['s'], entry_field_name='s')
        ts_mt = ts_mt.filter_cols(False)
        hl.export_vcf(
            ts_mt,
            output_vcf_paths[transmission_confidence],
            tabix=True,
        )
    return output_vcf_paths


def run_vep(
    out_ht_path: str,
    mt: hl.MatrixTable,
    vep_version: Optional[str],
    work_bucket: str,
    overwrite: bool = False,
) -> hl.Table:
    """
    Returns a table with a VEP annotation for each variant in the raw MatrixTable.
    :param mt: keyed by locus and allele, soft-filtered
    :param vep_version:
    :return: VEPed Table
    """
    if not overwrite and file_exists(out_ht_path):
        return hl.read_table(out_ht_path)
    vep_config_local = join(joint_calling.package_path(), 'vep-config.json')
    vep_config_gs = join(work_bucket, basename(vep_config_local))
    subprocess.run(
        f'gsutil cp {vep_config_local} {vep_config_gs}', check=False, shell=True
    )
    ht = mt.rows()
    ht = ht.filter(hl.len(ht.alleles) > 1)
    ht = hl.split_multi_hts(ht)
    ht = vep_or_lookup_vep(ht, vep_version=vep_version, vep_config_path=vep_config_gs)
    ht = ht.annotate_globals(version=f'v{vep_version}')
    ht.write(out_ht_path, overwrite=True)
    return ht


if __name__ == '__main__':
    main()  # pylint: disable=E1120
