import os
import sys
import pandas as pd
import numpy as np
import scipy
import scipy.stats
import pypeliner

import remixt.seqdataio


def infer_snp_genotype(data, base_call_error=0.005, call_threshold=0.9):
    """ Infer snp genotype based on binomial PMF

    Args:
        data (pandas.DataFrame): input snp data

    KwArgs:
        base_call_error (float): per base sequencing error
        call_threshold (float): posterior threshold for calling a genotype

    Input dataframe should have columns 'ref_count', 'alt_count'

    The operation is in-place, and the input dataframe after the call will
    have 'AA', 'AB', 'BB' columns, in addition to others.

    """

    data['total_count'] = data['ref_count'] + data['alt_count']

    data['likelihood_AA'] = scipy.stats.binom.pmf(data['alt_count'], data['total_count'], base_call_error)
    data['likelihood_AB'] = scipy.stats.binom.pmf(data['alt_count'], data['total_count'], 0.5)
    data['likelihood_BB'] = scipy.stats.binom.pmf(data['ref_count'], data['total_count'], base_call_error)
    data['evidence'] = data['likelihood_AA'] + data['likelihood_AB'] + data['likelihood_BB']

    data['posterior_AA'] = data['likelihood_AA'] / data['evidence']
    data['posterior_AB'] = data['likelihood_AB'] / data['evidence']
    data['posterior_BB'] = data['likelihood_BB'] / data['evidence']

    data['AA'] = (data['posterior_AA'] >= call_threshold) * 1
    data['AB'] = (data['posterior_AB'] >= call_threshold) * 1
    data['BB'] = (data['posterior_BB'] >= call_threshold) * 1


def read_snp_counts(seqdata_filename, chromosome, num_rows=1000000):
    """ Count reads for each SNP from sequence data

    Args:
        seqdata_filename (str): sequence data filename
        chromosome (str): chromosome for which to count reads

    KwArgs:
        num_rows (int): number of rows per chunk for streaming

    Returns:
        pandas.DataFrame: read counts per SNP

    Returned dataframe has columns 'position', 'ref_count', 'alt_count'

    """

    snp_counts = list()
    for alleles_chunk in remixt.seqdataio.read_allele_data(seqdata_filename, chromosome=chromosome, num_rows=num_rows):

        snp_counts_chunk = (
            alleles_chunk
            .groupby(['position', 'is_alt'])
            .size()
            .unstack()
            .fillna(0)
            .astype(int)
            .rename(columns=lambda a: {0:'ref_count', 1:'alt_count'}[a])
            .reset_index()
        )

        snp_counts.append(snp_counts_chunk)

    snp_counts = pd.concat(snp_counts, ignore_index=True).groupby('position').sum().reset_index()
    snp_counts.sort('position', inplace=True)

    return snp_counts

    
def infer_haps(haps_filename, seqdata_filename, chromosome, temp_directory, config):
    """ Infer haplotype blocks for a chromosome using shapeit

    Args:
        haps_filename (str): output haplotype data file
        seqdata_filename (str): input sequence data file
        chromosome (str): id of chromosome for which haplotype blocks will be inferred
        temp_directory (str): directory in which shapeit temp files will be stored
        config (dict): relavent shapeit parameters including thousand genomes paths

    The output haps file will contain haplotype blocks for each heterozygous SNP position. The
    file will be TSV format with the following columns:

        'chromosome': het snp chromosome
        'position': het snp position
        'allele': binary indicator for reference (0) vs alternate (1) allele
        'hap_label': label of the haplotype block
        'allele_id': binary indicator of the haplotype allele

    """
    
    def write_null():
        with open(haps_filename, 'w') as haps_file:
            haps_file.write('chromosome\tposition\tallele\thap_label\tallele_id\n')

    accepted_chromosomes = [str(a) for a in range(1, 23)] + ['X']
    if str(chromosome) not in accepted_chromosomes:
        write_null()
        return
    
    # Temporary directory for shapeit files
    try:
        os.makedirs(temp_directory)
    except OSError:
        pass

    # Impute 2 files for thousand genomes data by chromosome
    phased_chromosome = chromosome
    if chromosome == 'X':
        phased_chromosome = config['phased_chromosome_x']
    genetic_map_filename = config['genetic_map_template'].format(phased_chromosome)
    hap_filename = config['haplotypes_template'].format(phased_chromosome)
    legend_filename = config['legend_template'].format(phased_chromosome)

    # Call snps based on reference and alternate read counts from normal
    snp_counts_df = read_snp_counts(seqdata_filename, chromosome)

    if len(snp_counts_df) == 0:
        write_null()
        return

    infer_snp_genotype(snp_counts_df, config['sequencing_base_call_error'], config['het_snp_call_threshold'])

    # Remove ambiguous positions
    snp_counts_df = snp_counts_df[(snp_counts_df['AA'] == 1) | (snp_counts_df['AB'] == 1) | (snp_counts_df['BB'] == 1)]

    # Read snp positions from legend
    snps_df = pd.read_csv(legend_filename, compression='gzip', sep=' ', usecols=['position', 'a0', 'a1'])

    # Remove indels
    snps_df = snps_df[(snps_df['a0'].isin(['A', 'C', 'T', 'G'])) & (snps_df['a1'].isin(['A', 'C', 'T', 'G']))]

    # Merge data specific inferred genotype
    snps_df = snps_df.merge(snp_counts_df[['position', 'AA', 'AB', 'BB']], on='position', how='inner', sort=False)

    # Create genotype file required by shapeit
    snps_df['chr'] = chromosome
    snps_df['chr_pos'] = snps_df['chr'].astype(str) + ':' + snps_df['position'].astype(str)

    temp_gen_filename = os.path.join(temp_directory, 'snps.gen')
    snps_df.to_csv(temp_gen_filename, sep=' ', columns=['chr', 'chr_pos', 'position', 'a0', 'a1', 'AA', 'AB', 'BB'], index=False, header=False)

    # Create single sample file required by shapeit
    temp_sample_filename = os.path.join(temp_directory, 'snps.sample')
    with open(temp_sample_filename, 'w') as temp_sample_file:
        temp_sample_file.write('ID_1 ID_2 missing sex\n0 0 0 0\nUNR1 UNR1 0 2\n')

    # Run shapeit to create phased haplotype graph
    hgraph_filename = os.path.join(temp_directory, 'phased.hgraph')
    hgraph_logs_prefix = hgraph_filename + '.log'
    chr_x_flag = ''
    if chromosome == 'X':
        chr_x_flag = '--chrX'
    pypeliner.commandline.execute('shapeit', '-M', genetic_map_filename, '-R', hap_filename, legend_filename, config['sample_filename'],
                                  '-G', temp_gen_filename, temp_sample_filename, '--output-graph', hgraph_filename, chr_x_flag,
                                  '--no-mcmc', '-L', hgraph_logs_prefix)

    # Run shapeit to sample from phased haplotype graph
    sample_template = os.path.join(temp_directory, 'sampled.{0}')
    averaged_changepoints = None
    for s in range(int(config['shapeit_num_samples'])):
        sample_prefix = sample_template.format(s)
        sample_log_filename = sample_prefix + '.log'
        sample_haps_filename = sample_prefix + '.haps'
        sample_sample_filename = sample_prefix + '.sample'
        pypeliner.commandline.execute('shapeit', '-convert', '--input-graph', hgraph_filename, '--output-sample', 
                                      sample_prefix, '--seed', str(s), '-L', sample_log_filename)
        sample_haps = pd.read_csv(sample_haps_filename, sep=' ', header=None, 
                                  names=['id', 'id2', 'position', 'ref', 'alt', 'allele1', 'allele2'],
                                  usecols=['position', 'allele1', 'allele2'])
        sample_haps = sample_haps[sample_haps['allele1'] != sample_haps['allele2']]
        sample_haps['allele'] = sample_haps['allele1']
        sample_haps = sample_haps.drop(['allele1', 'allele2'], axis=1)
        sample_haps.set_index('position', inplace=True)
        sample_changepoints = sample_haps['allele'].diff().abs().astype(float).fillna(0.0)
        if averaged_changepoints is None:
            averaged_changepoints = sample_changepoints
        else:
            averaged_changepoints += sample_changepoints
        os.remove(sample_log_filename)
        os.remove(sample_haps_filename)
        os.remove(sample_sample_filename)
    averaged_changepoints /= float(config['shapeit_num_samples'])
    last_sample_haps = sample_haps

    # Identify changepoints recurrent across samples
    changepoint_confidence = np.maximum(averaged_changepoints, 1.0 - averaged_changepoints)

    # Create a list of labels for haplotypes between recurrent changepoints
    current_hap_label = 0
    hap_label = list()
    for x in changepoint_confidence:
        if x < float(config['shapeit_confidence_threshold']):
            current_hap_label += 1
        hap_label.append(current_hap_label)

    # Create the list of haplotypes
    haps = last_sample_haps
    haps['changepoint_confidence'] = changepoint_confidence
    haps['hap_label'] = hap_label

    haps.reset_index(inplace=True)

    haps['allele_id'] = 0

    haps_allele2 = haps.copy()
    haps_allele2['allele_id'] = 1
    haps_allele2['allele'] = 1 - haps_allele2['allele']

    haps = pd.concat([haps, haps_allele2], ignore_index=True)
    haps.sort(['position', 'allele_id'], inplace=True)

    haps['chromosome'] = chromosome

    haps = haps[['chromosome', 'position', 'allele', 'hap_label', 'allele_id']]

    haps.to_csv(haps_filename, sep='\t', index=False)


def count_allele_reads(seqdata_filename, haps, chromosome, segments):
    """ Count reads for each allele of haplotype blocks for a given chromosome

    Args:
        seqdata_filename (str): input sequence data file
        haps (pandas.DataFrame): input haplotype data
        chromosome (str): id of chromosome for which counts will be calculated
        segments (pandas.DataFrame): input genomic segments

    Input haps should have the following columns:

        'chromosome': het snp chromosome
        'position': het snp position
        'allele': binary indicator for reference (0) vs alternate (1) allele
        'hap_label': label of the haplotype block
        'allele_id': binary indicator of the haplotype allele

    Input segments should have columns 'start', 'end'.

    The output allele counts table will contain read counts for haplotype blocks within each segment.

        'chromosome': chromosome of the segment
        'start': start of the segment
        'end': end of the segment
        'hap_label': label of the haplotype block
        'allele_id': binary indicator of the haplotype allele
        'readcount': number of reads specific to haplotype block allele

    """

    # Select haps for given chromosome
    haps = haps[haps['chromosome'] == chromosome]

    # Merge haplotype information into read alleles table
    alleles = list()
    for alleles_chunk in remixt.seqdataio.read_allele_data(seqdata_filename, chromosome=chromosome, num_rows=1000000):
        alleles_chunk = alleles_chunk.merge(haps, left_on=['position', 'is_alt'], right_on=['position', 'allele'], how='inner')
        alleles.append(alleles_chunk)
    alleles = pd.concat(alleles, ignore_index=True)

    # Merge read start and end into read alleles table
    reads = next(remixt.seqdataio.read_read_data(seqdata_filename, chromosome=chromosome))
    alleles = alleles.merge(reads, left_on='fragment_id', right_index=True)

    # Arbitrarily assign a haplotype/allele label to each read
    alleles.drop_duplicates('fragment_id', inplace=True)

    # Sort in preparation for search, reindex to allow for subsequent merge
    segments = segments.sort('start').reset_index(drop=True)

    # Annotate segment for start and end of each read
    alleles.sort('start', inplace=True)
    alleles['segment_idx'] = remixt.segalg.find_contained(
        segments[['start', 'end']].values,
        alleles['start'].values
    )
    alleles.sort('end', inplace=True)
    alleles['end_segment_idx'] = remixt.segalg.find_contained(
        segments[['start', 'end']].values,
        alleles['end'].values
    )

    # Remove rows outside of the given segments
    alleles.dropna(subset=['segment_idx', 'end_segment_idx'], inplace=True)

    # Remove reads not contained within the same segment
    alleles = alleles[alleles['segment_idx'] == alleles['end_segment_idx']]

    # Drop unecessary columns
    alleles.drop(['start', 'end', 'end_segment_idx'], axis=1, inplace=True)

    # Merge segment start end, key for each segment (for given chromosome)
    alleles = alleles.merge(segments[['start', 'end']], left_on='segment_idx', right_index=True)

    # Count reads for each allele
    allele_counts = (
        alleles
        .set_index(['start', 'end', 'hap_label', 'allele_id'])
        .groupby(level=[0, 1, 2, 3])
        .size()
        .reset_index()
        .rename(columns={0:'readcount'})
    )

    # Add chromosome to output
    allele_counts['chromosome'] = chromosome

    return allele_counts


def create_allele_counts(segments, seqdata_filename, haps_filename):
    """ Create a table of read counts for alleles

    Args:
        segments (pandas.DataFrame): input segment data
        seqdata_filename (str): input sequence data file
        haps_filename (str): input haplotype data file

    Input segments should have columns 'chromosome', 'start', 'end'.

    The output allele counts table will contain read counts for haplotype blocks within each segment.

        'chromosome': chromosome of the segment
        'start': start of the segment
        'end': end of the segment
        'hap_label': label of the haplotype block
        'allele_id': binary indicator of the haplotype allele
        'readcount': number of reads specific to haplotype block allele

    """

    # Read haplotype block data
    haps = pd.read_csv(haps_filename, sep='\t', converters={'chromosome':str})

    # Count separately for each chromosome
    gp = segments.groupby('chromosome')

    # Table of allele counts, calculated for each group
    counts = [count_allele_reads(seqdata_filename, haps, chrom, segs.copy()) for chrom, segs in gp]
    counts = pd.concat(counts, ignore_index=True)

    return counts


def phase_segments(*allele_counts_tables):
    """ Phase haplotype blocks within segments

    Args:
        allele_counts_tables (list of pandas.DataFrame): input allele counts to be phased

    Returns:
        list of pandas.DataFrame: corresponding list of phased alelle counts

    The input allele counts table should contain columns 'chromosome', 'start', 'end', 
    'hap_label', 'allele_id', 'readcount'.

    The output phased allele count table will contain an additional column:

        'is_allele_a': indicator, is allele 'a' (1), is allele 'b' (0)

    """

    allele_phases = list()
    allele_diffs = list()

    for idx, allele_data in enumerate(allele_counts_tables):
        
        # Allele readcount table
        allele_data = allele_data.set_index(['chromosome', 'start', 'end', 'hap_label', 'allele_id'])['readcount'].unstack().fillna(0.0)
        allele_data = allele_data.astype(float)
        
        # Create major allele call
        allele_phase = allele_data.apply(np.argmax, axis=1)
        allele_phase.name = 'major_allele_id'
        allele_phase = allele_phase.reset_index()
        allele_phase['library_idx'] = idx
        allele_phases.append(allele_phase)

        # Calculate major minor allele read counts, and diff between them
        allele_data['major_readcount'] = allele_data.apply(np.max, axis=1)
        allele_data['minor_readcount'] = allele_data.apply(np.min, axis=1)
        allele_data['diff_readcount'] = allele_data['major_readcount'] - allele_data['minor_readcount']
        allele_data['total_readcount'] = allele_data['major_readcount'] + allele_data['minor_readcount']

        # Calculate normalized major and minor read counts difference per segment
        allele_diff = allele_data.groupby(level=[0, 1, 2])[['diff_readcount', 'total_readcount']].sum()
        allele_diff['norm_diff_readcount'] = allele_diff['diff_readcount'] / allele_diff['total_readcount']
        allele_diff = allele_diff[['norm_diff_readcount']]

        # Add to table for all librarys
        allele_diff.reset_index(inplace=True)
        allele_diff['library_idx'] = idx
        allele_diffs.append(allele_diff)

    allele_phases = pd.concat(allele_phases, ignore_index=True)
    allele_diffs = pd.concat(allele_diffs, ignore_index=True)

    def select_largest_diff(df):
        largest_idx = np.argmax(df['norm_diff_readcount'].values)
        return df['library_idx'].values[largest_idx]

    # For each segment, select the library with the largest difference between major and minor
    segment_library = allele_diffs.set_index(['chromosome', 'start', 'end']).groupby(level=[0, 1, 2]).apply(select_largest_diff)
    segment_library.name = 'library_idx'
    segment_library = segment_library.reset_index()

    # For each haplotype block in each segment, take the major allele call of the library
    # with the largest major minor difference and call it allele 'a'
    allele_phases = allele_phases.merge(segment_library, left_on=['chromosome', 'start', 'end', 'library_idx'], right_on=['chromosome', 'start', 'end', 'library_idx'], how='right')
    allele_phases = allele_phases[['chromosome', 'start', 'end', 'hap_label', 'major_allele_id']].rename(columns={'major_allele_id': 'allele_a_id'})

    # Create a list of phased allele count tables correspoinding to input tables
    phased_allele_counts = list()
    for allele_data in allele_counts_tables:

        # Add a boolean column denoting which allele is allele 'a'
        allele_data = allele_data.merge(allele_phases, left_on=['chromosome', 'start', 'end', 'hap_label'], right_on=['chromosome', 'start', 'end', 'hap_label'])
        allele_data['is_allele_a'] = (allele_data['allele_id'] == allele_data['allele_a_id']) * 1
        allele_data = allele_data[['chromosome', 'start', 'end', 'hap_label', 'allele_id', 'readcount', 'is_allele_a']]

        phased_allele_counts.append(allele_data)

    return phased_allele_counts
