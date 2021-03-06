#!/usr/bin/env python

"""
Creates a Table with all necessary annotations for the random forest model.
Annotations that are included:
    Features for RF:
        - InbreedingCoeff
        - variant_type
        - allele_type
        - n_alt_alleles
        - has_star
        - AS_QD
        - AS_pab_max
        - AS_MQRankSum
        - AS_SOR
        - AS_ReadPosRankSum
    Training sites (bool):
        - transmitted_singleton
        - fail_hard_filters - (ht.QD < 2) | (ht.FS > 60) | (ht.MQ < 30)
"""

from os.path import join
import logging
from typing import Optional

import click
import hail as hl

from gnomad.variant_qc.random_forest import median_impute_features

from joint_calling.utils import get_validation_callback
from joint_calling import utils, resources
from joint_calling import _version

logger = logging.getLogger('random_forest')
logger.setLevel('INFO')


FEATURES = [
    'allele_type',
    'AS_MQRankSum',
    'AS_pab_max',
    'AS_QD',
    'AS_ReadPosRankSum',
    'AS_SOR',
    'InbreedingCoeff',
    'n_alt_alleles',
    'variant_type',
]
INBREEDING_COEFF_HARD_CUTOFF = -0.3
INFO_FEATURES = [
    'AS_MQRankSum',
    'AS_pab_max',
    'AS_QD',
    'AS_ReadPosRankSum',
    'AS_SOR',
]
LABEL_COL = 'rf_label'
PREDICTION_COL = 'rf_prediction'
TRAIN_COL = 'rf_train'
TRUTH_DATA = ['hapmap', 'omni', 'mills', 'kgp_phase1_hc']


@click.command()
@click.version_option(_version.__version__)
@click.option(
    '--info-split-ht',
    'info_split_ht_path',
    required=True,
    callback=get_validation_callback(ext='ht', must_exist=True),
    help='path to info Table with split multiallelics '
    '(generated by generate_variant_qc_annotations.py --split-multiallelic)',
)
@click.option(
    '--freq-ht',
    'freq_ht_path',
    required=True,
    callback=get_validation_callback(ext='ht', must_exist=True),
    help='path to a Table with InbreedingCoeff (generated by generate_freq_data.py)',
)
@click.option(
    '--fam-stats-ht',
    'fam_stats_ht_path',
    callback=get_validation_callback(ext='ht'),
    help='optional path to a Table with trio stats '
    '(generated by generate_variant_qc_annotations.py)',
)
@click.option(
    '--allele-data-ht',
    'allele_data_ht_path',
    required=True,
    callback=get_validation_callback(ext='ht', must_exist=True),
    help='path to a Table with allele data '
    '(generated by generate_variant_qc_annotations.py)',
)
@click.option(
    '--qc-ac-ht',
    'qc_ac_ht_path',
    required=True,
    callback=get_validation_callback(ext='ht', must_exist=True),
    help='path to a Table with allele counts '
    '(generated by generate_variant_qc_annotations.py)',
)
@click.option(
    '--out-ht',
    'out_ht_path',
    required=True,
    callback=get_validation_callback(ext='ht'),
    help='Path to write the result',
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
    '--impute-features',
    'impute_features',
    is_flag=True,
    help='If set, feature imputation is performed',
)
@click.option(
    '--n-partitions',
    'n_partitions',
    type=click.INT,
    help='Desired base number of partitions for output tables',
    default=5000,
)
@click.option(
    '--use-adj-genotypes',
    'use_adj_genotypes',
    help='Use adj genotypes',
    is_flag=True,
)
@click.option(
    '--overwrite/--reuse',
    'overwrite',
    is_flag=True,
    help='if an intermediate or a final file exists, skip running the code '
    'that generates it.',
)
def main(  # pylint: disable=too-many-arguments,too-many-locals
    info_split_ht_path: str,
    freq_ht_path: str,
    fam_stats_ht_path: Optional[str],
    allele_data_ht_path: str,
    qc_ac_ht_path: str,
    out_ht_path: str,
    work_bucket: str,
    local_tmp_dir: str,
    impute_features: bool,
    n_partitions: int,
    use_adj_genotypes: bool,
    overwrite: bool,  # pylint: disable=unused-argument
):  # pylint: disable=missing-function-docstring
    local_tmp_dir = utils.init_hail('variant_qc_random_forest', local_tmp_dir)

    group = 'adj' if use_adj_genotypes else 'raw'
    ht = hl.read_table(info_split_ht_path)
    ht = ht.transmute(**ht.info)
    ht = ht.select('lowqual', 'AS_lowqual', 'FS', 'MQ', 'QD', *INFO_FEATURES)

    inbreeding_ht = hl.read_table(freq_ht_path)
    inbreeding_ht = inbreeding_ht.select(
        InbreedingCoeff=hl.if_else(
            hl.is_nan(inbreeding_ht.InbreedingCoeff),
            hl.null(hl.tfloat32),
            inbreeding_ht.InbreedingCoeff,
        )
    )
    if fam_stats_ht_path:
        fam_stats_ht = hl.read_table(fam_stats_ht_path)
        fam_stats_ht = fam_stats_ht.select(
            f'n_transmitted_{group}', f'ac_children_{group}'
        )
        ht = ht.annotate(
            **fam_stats_ht[ht.key],
        )

    logger.info('Annotating Table with all columns from multiple annotation Tables')
    truth_data_ht = resources.get_truth_ht()
    allele_data_ht = hl.read_table(allele_data_ht_path)
    qc_ac_ht = hl.read_table(qc_ac_ht_path)
    ht = ht.annotate(
        **inbreeding_ht[ht.key],
        **truth_data_ht[ht.key],
        **allele_data_ht[ht.key].allele_data,
        **qc_ac_ht[ht.key],
    )

    # Filter to only variants found in high quality samples and are not lowqual
    ht = ht.filter((ht[f'ac_qc_samples_{group}'] > 0) & ~ht.AS_lowqual)
    ht = ht.select(
        'a_index',
        'was_split',
        *FEATURES,
        *TRUTH_DATA,
        fail_hard_filters=(ht.QD < 2) | (ht.FS > 60) | (ht.MQ < 30),
        transmitted_singleton=(
            (ht[f'n_transmitted_{group}'] == 1) & (ht[f'ac_qc_samples_{group}'] == 2)
        )
        if fam_stats_ht_path is not None
        else hl.literal(False),
        singleton=(ht.ac_release_samples_raw == 1),
        ac_raw=ht.ac_qc_samples_raw,
        ac=ht.ac_release_samples_adj,
        ac_qc_samples_unrelated_raw=ht.ac_qc_samples_unrelated_raw,
    )

    ht = ht.repartition(n_partitions, shuffle=False)
    if work_bucket:
        checkpoint_path = join(work_bucket, 'rf-annotations-before-impute.ht')
        ht = ht.checkpoint(checkpoint_path, overwrite=True)

    if impute_features:
        ht = median_impute_features(ht, {'variant_type': ht.variant_type})

    ht.write(out_ht_path, overwrite=True)

    summary = ht.group_by(
        'omni',
        'mills',
        'transmitted_singleton',
    ).aggregate(n=hl.agg.count())
    logger.info('Summary of truth data annotations:')
    summary.show(20)


if __name__ == '__main__':
    main()  # pylint: disable=E1120
