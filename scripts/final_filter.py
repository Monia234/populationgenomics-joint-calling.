import argparse
import logging
import sys
from typing import Optional

import hail as hl
import click

from gnomad.resources.grch38.reference_data import telomeres_and_centromeres
from gnomad.utils.file_utils import file_exists
from gnomad.utils.filtering import add_filters_expr
from gnomad.utils.slack import slack_notifications
from gnomad.variant_qc.pipeline import INBREEDING_COEFF_HARD_CUTOFF

from joint_calling.utils import file_exists, get_validation_callback
from joint_calling import utils
from joint_calling import _version


logger = logging.getLogger('variant_qc_filtering')
logger.setLevel('INFO')


def generate_final_filter_ht(
    ht: hl.Table,
    model_name: str,
    score_name: str,
    ac0_filter_expr: hl.expr.BooleanExpression,
    ts_ac_filter_expr: hl.expr.BooleanExpression,
    mono_allelic_flag_expr: hl.expr.BooleanExpression,
    inbreeding_coeff_cutoff: float = INBREEDING_COEFF_HARD_CUTOFF,
    snp_bin_cutoff: int = None,
    indel_bin_cutoff: int = None,
    snp_score_cutoff: float = None,
    indel_score_cutoff: float = None,
    aggregated_bin_ht: Optional[hl.Table] = None,
    bin_id: Optional[str] = None,
    vqsr_ht: hl.Table = None,
) -> hl.Table:
    """
    Prepares finalized filtering model given a filtering HT from `rf.apply_rf_model` or VQSR and cutoffs for filtering.

    .. note::

        - `snp_bin_cutoff` and `snp_score_cutoff` are mutually exclusive, and one must be supplied.
        - `indel_bin_cutoff` and `indel_score_cutoff` are mutually exclusive, and one must be supplied.
        - If a `snp_bin_cutoff` or `indel_bin_cutoff` cutoff is supplied then an `aggregated_bin_ht` and `bin_id` must
          also be supplied to determine the SNP and indel scores to use as cutoffs from an aggregated bin Table like
          one created by `compute_grouped_binned_ht` in combination with `score_bin_agg`.

    :param ht: Filtering Table from `rf.apply_rf_model` or VQSR to prepare as the final filter Table
    :param model_name: Filtering model name to use in the 'filters' field (VQSR or RF)
    :param score_name: Name to use for the filtering score annotation. This will be used in place of 'score' in the
        release HT info struct and the INFO field of the VCF (e.g. RF or AS_VQSLOD)
    :param ac0_filter_expr: Expression that indicates if a variant should be filtered as allele count 0 (AC0)
    :param ts_ac_filter_expr: Allele count expression in `ht` to use as a filter for determining a transmitted singleton
    :param mono_allelic_flag_expr: Expression indicating if a variant is mono-allelic
    :param inbreeding_coeff_cutoff: InbreedingCoeff hard filter to use for variants
    :param snp_bin_cutoff: Bin cutoff to use for SNP variant QC filter. Can't be used with `snp_score_cutoff`
    :param indel_bin_cutoff: Bin cutoff to use for indel variant QC filter. Can't be used with `indel_score_cutoff`
    :param snp_score_cutoff: Score cutoff (e.g. RF probability or AS_VQSLOD) to use for SNP variant QC filter. Can't be used with `snp_bin_cutoff`
    :param indel_score_cutoff: Score cutoff (e.g. RF probability or AS_VQSLOD) to use for indel variant QC filter. Can't be used with `indel_bin_cutoff`
    :param aggregated_bin_ht: Table with aggregate counts of variants based on bins
    :param bin_id: Name of bin to use in 'bin_id' column of `aggregated_bin_ht` to use to determine probability cutoff
    :param vqsr_ht: If a VQSR HT is supplied a 'vqsr' annotation containing AS_VQSLOD, AS_culprit, NEGATIVE_TRAIN_SITE,
        and POSITIVE_TRAIN_SITE will be included in the returned Table
    :return: Finalized random forest Table annotated with variant filters
    """
    if snp_bin_cutoff is not None and snp_score_cutoff is not None:
        raise ValueError(
            "snp_bin_cutoff and snp_score_cutoff are mutually exclusive, please only supply one SNP filtering cutoff."
        )

    if indel_bin_cutoff is not None and indel_score_cutoff is not None:
        raise ValueError(
            "indel_bin_cutoff and indel_score_cutoff are mutually exclusive, please only supply one indel filtering cutoff."
        )

    if snp_bin_cutoff is None and snp_score_cutoff is None:
        raise ValueError(
            "One (and only one) of the parameters snp_bin_cutoff and snp_score_cutoff must be supplied."
        )

    if indel_bin_cutoff is None and indel_score_cutoff is None:
        raise ValueError(
            "One (and only one) of the parameters indel_bin_cutoff and indel_score_cutoff must be supplied."
        )

    if (snp_bin_cutoff is not None or indel_bin_cutoff is not None) and (
        aggregated_bin_ht is None or bin_id is None
    ):
        raise ValueError(
            "If using snp_bin_cutoff or indel_bin_cutoff, both aggregated_bin_ht and bin_id must be supplied"
        )

    # Determine SNP and indel score cutoffs if given bin instead of score
    if snp_bin_cutoff:
        snp_score_cutoff = aggregated_bin_ht.aggregate(
            hl.agg.filter(
                aggregated_bin_ht.snv
                & (aggregated_bin_ht.bin_id == bin_id)
                & (aggregated_bin_ht.bin == snp_bin_cutoff),
                hl.agg.min(aggregated_bin_ht.min_score),
            )
        )
        snp_cutoff_global = hl.struct(bin=snp_bin_cutoff, min_score=snp_score_cutoff)

    if indel_bin_cutoff:
        indel_score_cutoff = aggregated_bin_ht.aggregate(
            hl.agg.filter(
                ~aggregated_bin_ht.snv
                & (aggregated_bin_ht.bin_id == bin_id)
                & (aggregated_bin_ht.bin == indel_bin_cutoff),
                hl.agg.min(aggregated_bin_ht.min_score),
            )
        )
        indel_cutoff_global = hl.struct(
            bin=indel_bin_cutoff, min_score=indel_score_cutoff
        )

    min_score = ht.aggregate(hl.agg.min(ht.score))
    max_score = ht.aggregate(hl.agg.max(ht.score))

    if snp_score_cutoff:
        if snp_score_cutoff < min_score or snp_score_cutoff > max_score:
            raise ValueError("snp_score_cutoff is not within the range of score.")
        snp_cutoff_global = hl.struct(min_score=snp_score_cutoff)

    if indel_score_cutoff:
        if indel_score_cutoff < min_score or indel_score_cutoff > max_score:
            raise ValueError("indel_score_cutoff is not within the range of score.")
        indel_cutoff_global = hl.struct(min_score=indel_score_cutoff)

    logger.info(
        f"Using a SNP score cutoff of {snp_score_cutoff} and an indel score cutoff of {indel_score_cutoff}."
    )

    # Add filters to HT
    filters = dict()

    if ht.any(hl.is_missing(ht.score)):
        ht.filter(hl.is_missing(ht.score)).show()
        raise ValueError("Missing Score!")

    filters[model_name] = (
        hl.is_missing(ht.score)
        | (
            hl.is_snp(ht.alleles[0], ht.alleles[1])
            & (ht.score < snp_cutoff_global.min_score)
        )
        | (
            ~hl.is_snp(ht.alleles[0], ht.alleles[1])
            & (ht.score < indel_cutoff_global.min_score)
        )
    )

    filters["InbreedingCoeff"] = hl.or_else(
        ht.InbreedingCoeff < inbreeding_coeff_cutoff, False
    )
    filters["AC0"] = ac0_filter_expr

    annotations_expr = dict()
    if model_name == "RF":
        # Fix annotations for release
        annotations_expr = annotations_expr.update(
            {
                "positive_train_site": hl.or_else(ht.positive_train_site, False),
                "rf_tp_probability": ht.rf_probability["TP"],
            }
        )
    annotations_expr.update(
        {
            "transmitted_singleton": hl.or_missing(
                ts_ac_filter_expr, ht.transmitted_singleton
            )
        }
    )
    if "feature_imputed" in ht.row:
        annotations_expr.update(
            {
                x: hl.or_missing(~ht.feature_imputed[x], ht[x])
                for x in [f for f in ht.row.feature_imputed]
            }
        )

    ht = ht.transmute(
        filters=add_filters_expr(filters=filters),
        monoallelic=mono_allelic_flag_expr,
        **{score_name: ht.score},
        **annotations_expr,
    )

    bin_names = [x for x in ht.row if x.endswith("bin")]
    bin_names = [
        (
            x,
            x.split("adj_")[0] + x.split("adj_")[1]
            if len(x.split("adj_")) == 2
            else "raw_" + x,
        )
        for x in bin_names
    ]
    ht = ht.transmute(**{j: ht[i] for i, j in bin_names})

    ht = ht.annotate_globals(
        bin_stats=hl.struct(**{j: ht.bin_stats[i] for i, j in bin_names}),
        filtering_model=hl.struct(
            model_name=model_name,
            score_name=score_name,
            snv_cutoff=snp_cutoff_global,
            indel_cutoff=indel_cutoff_global,
        ),
        inbreeding_coeff_cutoff=inbreeding_coeff_cutoff,
    )
    if vqsr_ht:
        vqsr = vqsr_ht[ht.key]
        ht = ht.annotate(
            vqsr=hl.struct(
                AS_VQSLOD=vqsr.info.AS_VQSLOD,
                AS_culprit=vqsr.info.AS_culprit,
                NEGATIVE_TRAIN_SITE=vqsr.info.NEGATIVE_TRAIN_SITE,
                POSITIVE_TRAIN_SITE=vqsr.info.POSITIVE_TRAIN_SITE,
            ),
            SOR=vqsr.info.SOR,  # NOTE: This was required for v3.1, we now compute this in `get_site_info_expr`
        )

    ht = ht.drop("AS_culprit")

    return ht


# RF or VQSR score bin to use as cutoff for SNPs. Value should be between 1 and 100
snp_bin_cutoff = None
# RF or VQSR score to use as cutoff for SNPs
snp_score_cutoff = None
# RF or VQSR score bin to use as cutoff for indels. Value should be between 1 and 100
indel_bin_cutoff = None
# RF or VQSR score to use as cutoff for indels
indel_score_cutoff = None
# Filter centromeres and telomeres from final filter Table
filter_centromere_telomere = True


@click.command()
@click.version_option(_version.__version__)
@click.option(
    '--info-ht',
    'info_split_ht_path',
    required=True,
    callback=get_validation_callback(ext='ht', must_exist=True),
    help='path to info Table with split multiallelics '
    '(generated by generate_qc_annotations.py --split-multiallelic)',
)
@click.option(
    '--model-id', 'model_id', help="Filtering model ID to use."
)
@click.option(
    '--model-name', 'model_name',
    help="Filtering model name to use in the filters field. Either 'VQSR' or 'RF'.",
    choices=["VQSR", "RF"],
)
@click.option(
    "--score-name", 'score_name',
    help=(
        "What to rename the filtering score annotation. This will be used in place of 'score' in the "
        "release HT info struct and the INFO field of the VCF (e.g. 'RF', 'AS_VQSLOD')."
    ),
)
@click.option(
    "--inbreeding-coeff-threshold",
    "inbreeding_coeff_threshold",
    help="InbreedingCoeff hard filter to use for variants.",
    type=float,
    default=INBREEDING_COEFF_HARD_CUTOFF,
)
@click.option(  # NOTE: This was required for v3.1 to grab the SOR annotation, we now compute this in `get_site_info_expr`
    "--vqsr-model-id",
    'vqsr_model_id',
    help=(
        "If a VQSR model ID is provided, a 'vqsr' annotation will be added to the final filter Table containing AS_VQSLOD "
        ", AS_culprit, NEGATIVE_TRAIN_SITE and POSITIVE_TRAIN_SITE."
    ),
    default="vqsr_alleleSpecificTrans",
    type=click.Choice(["vqsr_classic", "vqsr_alleleSpecific", "vqsr_alleleSpecificTrans"]),
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
def main(
    model_id,
    model_name,
    score_name,
    inbreeding_coeff_threshold,
    vqsr_model_id,
    work_bucket: str,
    local_tmp_dir: str,
    overwrite,
):
    """
    Filter
    """
    local_tmp_dir = utils.init_hail('variant_qc_finalize', local_tmp_dir)
    
    ht = get_score_bins(model_id, aggregated=False).ht()
    if args.filter_centromere_telomere:
        ht = ht.filter(~hl.is_defined(telomeres_and_centromeres.ht()[ht.locus]))

    info_ht = get_info(split=True).ht()
    ht = ht.filter(~info_ht[ht.key].AS_lowqual)

    if args.model_id.startswith("vqsr_"):
        ht = ht.drop("info")

    freq_ht = get_freq().ht()
    ht = ht.annotate(InbreedingCoeff=freq_ht[ht.key].InbreedingCoeff)
    freq_idx = freq_ht[ht.key]
    aggregated_bin_path = get_score_bins(args.model_id, aggregated=True).path
    if not file_exists(aggregated_bin_path):
        sys.exit(
            f"Could not find binned HT for model: {args.model_id} ({aggregated_bin_path}). Please run create_ranked_scores.py for that hash."
        )
    aggregated_bin_ht = get_score_bins(args.model_id, aggregated=True).ht()

    ht = generate_final_filter_ht(
        ht,
        args.model_name,
        args.score_name,
        ac0_filter_expr=freq_idx.freq[0].AC == 0,
        ts_ac_filter_expr=freq_idx.freq[1].AC == 1,
        mono_allelic_flag_expr=(freq_idx.freq[1].AF == 1) | (freq_idx.freq[1].AF == 0),
        snp_bin_cutoff=args.snp_bin_cutoff,
        indel_bin_cutoff=args.indel_bin_cutoff,
        snp_score_cutoff=args.snp_score_cutoff,
        indel_score_cutoff=args.indel_score_cutoff,
        inbreeding_coeff_cutoff=args.inbreeding_coeff_threshold,
        aggregated_bin_ht=aggregated_bin_ht,
        bin_id="bin",
        vqsr_ht=get_vqsr_filters(args.vqsr_model_id, split=True).ht()
        if args.vqsr_model_id
        else None,
    )
    ht = ht.annotate_globals(
        filtering_model=ht.filtering_model.annotate(model_id=args.model_id,)
    )
    if args.model_id.startswith("vqsr_"):
        ht = ht.annotate_globals(
            filtering_model=ht.filtering_model.annotate(
                snv_training_variables=[
                    "AS_QD",
                    "AS_MQRankSum",
                    "AS_ReadPosRankSum",
                    "AS_FS",
                    "AS_SOR",
                    "AS_MQ",
                ],
                indel_training_variables=[
                    "AS_QD",
                    "AS_MQRankSum",
                    "AS_ReadPosRankSum",
                    "AS_FS",
                    "AS_SOR",
                ],
            )
        )
    else:
        ht = ht.annotate_globals(
            filtering_model=ht.filtering_model.annotate(
                snv_training_variables=ht.features,
                indel_training_variables=ht.features,
            )
        )

    ht.write(final_filter.path, args.overwrite)

    final_filter_ht = final_filter.ht()
    final_filter_ht.summarize()


if __name__ == '__main__':
    main()  # pylint: disable=E1120
