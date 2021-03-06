"""
Variant QC Hail-query jobs
"""

import uuid
from os.path import join
from typing import List, Optional, Dict, Tuple
import logging
import hailtop.batch as hb
from hailtop.batch.job import Job

from joint_calling import utils, dataproc
from joint_calling.dataproc import get_cluster
from joint_calling.jobs.vqsr import add_vqsr_jobs

logger = logging.getLogger(__file__)
logging.basicConfig(format='%(levelname)s (%(name)s %(lineno)s): %(message)s')
logger.setLevel(logging.INFO)


def add_variant_qc_jobs(
    b: hb.Batch,
    work_bucket: str,
    web_bucket: str,
    raw_combined_mt_path: str,
    hard_filter_ht_path: str,
    meta_ht_path: str,
    out_filtered_combined_mt_path: str,
    out_filtered_vcf_ptrn_path: str,
    sample_count: int,
    ped_file: Optional[str],
    overwrite: bool,
    vqsr_params_d: Dict,
    scatter_count: int,
    is_test: bool,
    project_name: str,
    depends_on: Optional[List[Job]] = None,
    run_rf: bool = False,
) -> List[Job]:
    """
    Add variant QC Hail-query jobs
    """
    rf_bucket = join(work_bucket, 'rf')
    vqsr_bucket = join(work_bucket, 'vqsr')

    # Starting 3 clusters to work in parallel. The last one is long, to submit
    # further jobs
    cluster1 = get_cluster(
        b, f'VarQC 1', scatter_count, is_test=is_test, depends_on=depends_on
    )
    cluster2 = get_cluster(
        b, f'VarQC 2', scatter_count, is_test=is_test, depends_on=depends_on
    )
    cluster3 = get_cluster(
        b, f'VarQC 3', scatter_count, is_test=is_test, depends_on=depends_on, long=True
    )

    fam_stats_ht_path = join(work_bucket, 'fam-stats.ht') if ped_file else None
    allele_data_ht_path = join(work_bucket, 'allele-data.ht')
    qc_ac_ht_path = join(work_bucket, 'qc-ac.ht')
    rf_result_ht_path = None

    job_name = 'Var QC: generate info'
    info_ht_path = join(work_bucket, 'info.ht')
    info_split_ht_path = join(work_bucket, 'info-split.ht')
    if any(
        not utils.can_reuse(fp, overwrite) for fp in [info_ht_path, info_split_ht_path]
    ):
        info_job = cluster1.add_job(
            f'{utils.SCRIPTS_DIR}/generate_info_ht.py --overwrite '
            f'--mt {raw_combined_mt_path} '
            f'--out-info-ht {info_ht_path} '
            f'--out-split-info-ht {info_split_ht_path}',
            job_name=job_name,
        )
    else:
        info_job = b.new_job(f'{job_name} [reuse]')
    if depends_on:
        info_job.depends_on(*depends_on)

    job_name = 'Var QC: generate annotations'
    if any(
        not utils.can_reuse(fp, overwrite)
        for fp in [allele_data_ht_path, qc_ac_ht_path]
        + ([fam_stats_ht_path] if fam_stats_ht_path else [])
    ):
        var_qc_anno_job = cluster2.add_job(
            f'{utils.SCRIPTS_DIR}/generate_variant_qc_annotations.py '
            + f'{"--overwrite " if overwrite else ""}'
            + f'--mt {raw_combined_mt_path} '
            + f'--hard-filtered-samples-ht {hard_filter_ht_path} '
            + f'--meta-ht {meta_ht_path} '
            + f'--out-allele-data-ht {allele_data_ht_path} '
            + f'--out-qc-ac-ht {qc_ac_ht_path} '
            + (f'--out-fam-stats-ht {fam_stats_ht_path} ' if ped_file else '')
            + (f'--fam-file {ped_file} ' if ped_file else '')
            + f'--bucket {work_bucket} '
            + f'--n-partitions {scatter_count * 25}',
            job_name=job_name,
        )
        if depends_on:
            var_qc_anno_job.depends_on(*depends_on)
    else:
        var_qc_anno_job = b.new_job(f'{job_name} [reuse]')

    job_name = 'Var QC: generate frequencies'
    freq_ht_path = join(work_bucket, 'frequencies.ht')
    if overwrite or not utils.file_exists(freq_ht_path):
        freq_job = cluster3.add_job(
            f'{utils.SCRIPTS_DIR}/generate_freq_data.py --overwrite '
            f'--mt {raw_combined_mt_path} '
            f'--hard-filtered-samples-ht {hard_filter_ht_path} '
            f'--meta-ht {meta_ht_path} '
            f'--out-ht {freq_ht_path} '
            f'--bucket {work_bucket} ',
            job_name=job_name,
        )
        if depends_on:
            freq_job.depends_on(*depends_on)
    else:
        freq_job = b.new_job(f'{job_name} [reuse]')

    job_name = 'Var QC: create RF annotations'
    rf_annotations_ht_path = join(work_bucket, 'rf-annotations.ht')
    if overwrite or not utils.file_exists(rf_annotations_ht_path):
        rf_anno_job = cluster3.add_job(
            f'{utils.SCRIPTS_DIR}/create_rf_annotations.py --overwrite '
            f'--info-split-ht {info_split_ht_path} '
            f'--freq-ht {freq_ht_path} '
            + (f'--fam-stats-ht {fam_stats_ht_path} ' if fam_stats_ht_path else '')
            + f'--allele-data-ht {allele_data_ht_path} '
            f'--qc-ac-ht {qc_ac_ht_path} '
            f'--bucket {work_bucket} '
            f'--use-adj-genotypes '
            f'--out-ht {rf_annotations_ht_path} '
            + f'--n-partitions {scatter_count * 25}',
            job_name=job_name,
        )
        rf_anno_job.depends_on(freq_job, var_qc_anno_job, info_job)
    else:
        rf_anno_job = b.new_job(f'{job_name} [reuse]')

    if run_rf:
        cluster = get_cluster(
            b,
            'RF',
            scatter_count,
            is_test=is_test,
            long=True,
            depends_on=[rf_anno_job],
        )

        job_name = 'Random forest'
        rf_result_ht_path = join(work_bucket, 'rf-result.ht')
        rf_model_id = f'rf_{str(uuid.uuid4())[:8]}'
        if overwrite or not utils.file_exists(rf_result_ht_path):
            rf_job = cluster.add_job(
                f'{utils.SCRIPTS_DIR}/random_forest.py --overwrite '
                f'--annotations-ht {rf_annotations_ht_path} '
                f'--bucket {work_bucket} '
                f'--use-adj-genotypes '
                f'--out-results-ht {rf_result_ht_path} '
                f'--out-model-id {rf_model_id} ',
                job_name=job_name,
            )
            rf_job.depends_on(rf_anno_job)
        else:
            rf_job = b.new_job(f'{job_name} [reuse]')

        eval_job, final_filter_ht_path = add_rf_eval_jobs(
            b=b,
            dataproc_cluster=cluster,
            combined_mt_path=raw_combined_mt_path,
            info_split_ht_path=info_split_ht_path,
            rf_result_ht_path=rf_result_ht_path,
            rf_annotations_ht_path=rf_annotations_ht_path,
            fam_stats_ht_path=fam_stats_ht_path,
            freq_ht_path=freq_ht_path,
            rf_model_id=rf_model_id,
            work_bucket=rf_bucket,
            overwrite=overwrite,
            depends_on=[rf_job, freq_job, rf_anno_job],
        )

    else:
        vqsred_vcf_path = join(vqsr_bucket, 'output.vcf.gz')
        if overwrite or not utils.file_exists(vqsred_vcf_path):
            vqsr_vcf_job = add_vqsr_jobs(
                b,
                combined_mt_path=raw_combined_mt_path,
                hard_filter_ht_path=hard_filter_ht_path,
                meta_ht_path=meta_ht_path,
                gvcf_count=sample_count,
                work_bucket=vqsr_bucket,
                web_bucket=join(web_bucket, 'vqsr'),
                depends_on=depends_on or [],
                vqsr_params_d=vqsr_params_d,
                scatter_count=scatter_count,
                output_vcf_path=vqsred_vcf_path,
                overwrite=overwrite,
            )
        else:
            vqsr_vcf_job = b.new_job('AS-VQSR [reuse]')

        final_filter_ht_path = join(vqsr_bucket, 'final-filter.ht')

        cluster = get_cluster(
            b,
            'VQSR eval',
            scatter_count,
            is_test=is_test,
            long=True,
            depends_on=[rf_anno_job, vqsr_vcf_job],
        )
        eval_job = add_vqsr_eval_jobs(
            b=b,
            dataproc_cluster=cluster,
            combined_mt_path=raw_combined_mt_path,
            rf_annotations_ht_path=rf_annotations_ht_path,
            info_split_ht_path=info_split_ht_path,
            final_gathered_vcf_path=vqsred_vcf_path,
            rf_result_ht_path=rf_result_ht_path,
            fam_stats_ht_path=fam_stats_ht_path,
            freq_ht_path=freq_ht_path,
            work_bucket=vqsr_bucket,
            analysis_bucket=join(web_bucket, 'vqsr'),
            overwrite=overwrite,
            vqsr_vcf_job=vqsr_vcf_job,
            rf_anno_job=rf_anno_job,
            output_ht_path=final_filter_ht_path,
        )
        eval_job.depends_on(vqsr_vcf_job, rf_anno_job, info_job)

    job_name = 'Making final MT'
    if not utils.can_reuse(out_filtered_combined_mt_path, overwrite):
        final_mt_j = cluster.add_job(
            f'{utils.SCRIPTS_DIR}/make_finalised_mt.py --overwrite '
            f'--mt {raw_combined_mt_path} '
            f'--final-filter-ht {final_filter_ht_path} '
            f'--freq-ht {freq_ht_path} '
            f'--info-ht {info_split_ht_path} '
            f'--out-mt {out_filtered_combined_mt_path} '
            f'--meta-ht {meta_ht_path} ',
            job_name=job_name,
        )
        final_mt_j.depends_on(eval_job)
    else:
        final_mt_j = b.new_job(f'{job_name} [reuse]')

    job_name = f'Making final VCF: prepare HT'
    logger.info(job_name)
    export_ht_path = join(work_bucket, 'export_vcf.ht')
    export_vcf_header_txt = join(work_bucket, 'export_vcf_header.txt')
    if not utils.can_reuse([export_ht_path, export_vcf_header_txt], overwrite):
        final_ht_j = cluster.add_job(
            f'{utils.SCRIPTS_DIR}/release_vcf_prepare_ht.py '
            f'--mt {out_filtered_combined_mt_path} '
            f'--out-ht {export_ht_path} '
            f'--out-vcf-header-txt {export_vcf_header_txt}',
            job_name=job_name,
        )
    else:
        final_ht_j = b.new_job(f'{job_name} [reuse]')
    final_ht_j.depends_on(final_mt_j)

    jobs = []
    for chrom in list(map(str, range(1, 22 + 1))) + ['X', 'Y']:
        job_name = f'Making final VCF: HT to VCF for chr{chrom}'
        logger.info(job_name)
        vcf_path = out_filtered_vcf_ptrn_path.format(CHROM=chrom)
        if not utils.can_reuse([vcf_path], overwrite):
            j = cluster.add_job(
                f'{utils.SCRIPTS_DIR}/release_vcf_export_chrom.py '
                f'--ht {export_ht_path} '
                f'--vcf-header-txt {export_vcf_header_txt} '
                f'--out-vcf {vcf_path} '
                f'--name {project_name} '
                f'--chromosome chr{chrom}',
                job_name=job_name,
            )
        else:
            j = b.new_job(f'{job_name} [reuse]')
        jobs.append(j)
        j.depends_on(final_ht_j)
    return jobs


def add_rf_eval_jobs(
    b: hb.Batch,
    dataproc_cluster: dataproc.DataprocCluster,
    combined_mt_path: str,
    info_split_ht_path: str,
    rf_result_ht_path: str,
    rf_annotations_ht_path: str,
    fam_stats_ht_path: Optional[str],
    freq_ht_path: str,
    rf_model_id: str,
    work_bucket: str,
    overwrite: bool,
    depends_on: Optional[List[Job]] = None,
) -> Tuple[Job, str]:
    """
    Make jobs that do evaluation RF model and applies the final filters

    Returns the final_filter Job object and the path to the final filter HT
    """
    job_name = 'RF: evaluation'
    score_bin_ht_path = join(work_bucket, 'rf-score-bin.ht')
    score_bin_agg_ht_path = join(work_bucket, 'rf-score-agg-bin.ht')
    if overwrite or not utils.file_exists(score_bin_ht_path):
        eval_job = dataproc_cluster.add_job(
            f'{utils.SCRIPTS_DIR}/evaluation.py --overwrite '
            f'--mt {combined_mt_path} '
            f'--rf-annotations-ht {rf_annotations_ht_path} '
            f'--info-split-ht {info_split_ht_path} '
            + (f'--fam-stats-ht {fam_stats_ht_path} ' if fam_stats_ht_path else '')
            + f'--rf-results-ht {rf_result_ht_path} '
            f'--bucket {work_bucket} '
            f'--out-bin-ht {score_bin_ht_path} '
            f'--out-aggregated-bin-ht {score_bin_agg_ht_path} '
            f'--run-sanity-checks ',
            job_name=job_name,
        )
        if depends_on:
            eval_job.depends_on(*depends_on)
    else:
        eval_job = b.new_job(f'{job_name} [reuse]')

    job_name = 'RF: final filter'
    final_filter_ht_path = join(work_bucket, 'final-filter.ht')
    if overwrite or not utils.file_exists(final_filter_ht_path):
        final_filter_job = dataproc_cluster.add_job(
            f'{utils.SCRIPTS_DIR}/final_filter.py --overwrite '
            f'--out-final-filter-ht {final_filter_ht_path} '
            f'--model-id {rf_model_id} '
            f'--model-name RF '
            f'--score-name RF '
            f'--info-split-ht {info_split_ht_path} '
            f'--freq-ht {freq_ht_path} '
            f'--score-bin-ht {score_bin_ht_path} '
            f'--score-bin-agg-ht {score_bin_agg_ht_path} ' + f'--bucket {work_bucket} ',
            job_name=job_name,
        )
        final_filter_job.depends_on(eval_job)
    else:
        final_filter_job = b.new_job(f'{job_name} [reuse]')

    return final_filter_job, final_filter_ht_path


def add_vqsr_eval_jobs(
    b: hb.Batch,
    dataproc_cluster: dataproc.DataprocCluster,
    combined_mt_path: str,
    rf_annotations_ht_path: str,
    info_split_ht_path: str,
    final_gathered_vcf_path: str,
    rf_result_ht_path: Optional[str],
    fam_stats_ht_path: Optional[str],
    freq_ht_path: str,
    work_bucket: str,
    analysis_bucket: str,  # pylint: disable=unused-argument
    overwrite: bool,
    vqsr_vcf_job: Job,
    rf_anno_job: Job,
    output_ht_path: str,
) -> Job:
    """
    Make jobs that do evaluation VQSR model and applies the final filters

    Returns the final_filter Job object and the path to the final filter HT
    """
    job_name = 'AS-VQSR: load_vqsr'
    vqsr_filters_split_ht_path = join(work_bucket, 'vqsr-filters-split.ht')
    if overwrite or not utils.file_exists(vqsr_filters_split_ht_path):
        load_vqsr_job = dataproc_cluster.add_job(
            f'{utils.SCRIPTS_DIR}/load_vqsr.py --overwrite '
            f'--split-multiallelic '
            f'--out-path {vqsr_filters_split_ht_path} '
            f'--vqsr-vcf-path {final_gathered_vcf_path} '
            f'--bucket {work_bucket} ',
            job_name=job_name,
        )
        load_vqsr_job.depends_on(vqsr_vcf_job)
    else:
        load_vqsr_job = b.new_job(f'{job_name} [reuse]')

    job_name = 'AS-VQSR: evaluation'
    score_bin_ht_path = join(work_bucket, 'vqsr-score-bin.ht')
    score_bin_agg_ht_path = join(work_bucket, 'vqsr-score-agg-bin.ht')
    if (
        overwrite
        or not utils.file_exists(score_bin_ht_path)
        or not utils.file_exists(score_bin_agg_ht_path)
    ):
        eval_job = dataproc_cluster.add_job(
            f'{utils.SCRIPTS_DIR}/evaluation.py --overwrite '
            f'--mt {combined_mt_path} '
            f'--rf-annotations-ht {rf_annotations_ht_path} '
            f'--info-split-ht {info_split_ht_path} '
            + (f'--fam-stats-ht {fam_stats_ht_path} ' if fam_stats_ht_path else '')
            + (
                f'--rf-result-ht {rf_result_ht_path} '
                if (rf_annotations_ht_path and rf_result_ht_path)
                else ''
            )
            + f'--vqsr-filters-split-ht {vqsr_filters_split_ht_path} '
            f'--bucket {work_bucket} '
            f'--out-bin-ht {score_bin_ht_path} '
            f'--out-aggregated-bin-ht {score_bin_agg_ht_path} '
            f'--run-sanity-checks ',
            job_name=job_name,
        )
        eval_job.depends_on(load_vqsr_job, rf_anno_job)
    else:
        eval_job = b.new_job(f'{job_name} [reuse]')

    job_name = 'AS-VQSR: final filter'
    vqsr_model_id = 'vqsr_model'
    if not utils.file_exists(output_ht_path):
        final_filter_job = dataproc_cluster.add_job(
            f'{utils.SCRIPTS_DIR}/final_filter.py --overwrite '
            f'--out-final-filter-ht {output_ht_path} '
            f'--vqsr-filters-split-ht {vqsr_filters_split_ht_path} '
            f'--model-id {vqsr_model_id} '
            f'--model-name VQSR '
            f'--score-name AS_VQSLOD '
            f'--info-split-ht {info_split_ht_path} '
            f'--freq-ht {freq_ht_path} '
            f'--score-bin-ht {score_bin_ht_path} '
            f'--score-bin-agg-ht {score_bin_agg_ht_path} '
            f'--bucket {work_bucket} ',
            job_name=job_name,
        )
        final_filter_job.depends_on(eval_job)
    else:
        final_filter_job = b.new_job(f'{job_name} [reuse]')
    return final_filter_job
