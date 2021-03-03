#!/bin/bash

timestamp() {
  date "+%Y%m%d-%H%M%S"
}

# Submit combiner job for a first set of samples
hailctl dataproc submit cpg-qc-cluster \
  --region australia-southeast1 \
  --pyfiles libs/libs.zip \
  scripts/combine_gvcfs.py \
  --sample-map    gs://cpg-fewgenomes-temporary/cpg-qc/50genomes-gcs-au-round1.csv \
  --out-mt        gs://cpg-fewgenomes-test/cpg-qc/v1/$(timestamp)/50genomes.mt \
  --bucket        gs://cpg-fewgenomes-temporary/work/vcf-combiner/v1/$(timestamp)/ \
  --local-tmp-dir ~/tmp/cpg-qc/vcf-combiner/v1/$(timestamp)/ \
  --hail-billing  fewgenomes
