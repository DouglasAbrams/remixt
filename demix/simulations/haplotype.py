import itertools
import os
import pandas as pd
import numpy as np
import gzip


def create_sim_alleles(haplotypes_template, legend_template, chromosomes, recomb_rate=20.0/1.e8):
    """ Create simulated alleles from 1000 genomes SNPs

    Args:
        haplotypes_template (str): template for 1000 genomes haplotypes filename
        legend_template (str): template for 1000 genomes legend filename
        chromosomes (str): chromosomes to simulate

    KwArgs:
        recomb_rate (float): recombination rate per nt

    Returns:
        pandas.DataFrame

    """
    sim_alleles = list()

    for chromosome in chromosomes:

        hap_filename = haplotypes_template.format(chromosome)
        legend_filename = legend_template.format(chromosome)

        data = pd.read_csv(gzip.open(legend_filename, 'r'), sep=' ', usecols=['position', 'a0', 'a1'])

        with gzip.open(hap_filename, 'r') as hap_file:
            num_1kg_individuals = len(hap_file.readline().split()) / 2

        chromosome_length = data['position'].max() + 1000

        num_recombinations = int(np.ceil(recomb_rate * chromosome_length))

        # Randomly simulate recombinations
        recomb_positions = np.random.random_integers(1, chromosome_length - 1, num_recombinations)
        recomb_positions.sort()

        # Randomly select individuals for each recombinated region
        recomb_individuals = np.random.random_integers(0, num_1kg_individuals - 1, num_recombinations + 1)

        # Recombination regions
        recomb_start = np.array([0] + list(recomb_positions))
        recomb_end = np.array(list(recomb_positions) + [chromosome_length])

        # Add selected individual to legend table
        data['individual'] = -1
        for start, end, individual in itertools.izip(recomb_start, recomb_end, recomb_individuals):
            data.loc[(data['position'] >= start) & (data['position'] < end), 'individual'] = individual
        assert np.all(data['individual'] >= 0)

        # Columns to read from large haplotype matrix 
        individual_cols = np.concatenate([
            data['individual'].unique() * 2,
            data['individual'].unique() * 2 + 1,
        ])
        individual_cols.sort()

        # Columns of the in memory matrix which contains a subset of the original columns
        individual_idx = np.searchsorted(np.sort(data['individual'].unique()), data['individual'])
        individual_idx_0 = individual_idx * 2
        individual_idx_1 = individual_idx * 2 + 1

        # Select nucleotide codes based on individual
        hap_data = pd.read_csv(hap_filename, compression='gzip', sep=' ', dtype=np.uint8, header=None, names=xrange(num_1kg_individuals*2), usecols=individual_cols).values
        data['is_alt_0'] = hap_data[data.index.values,individual_idx_0]
        data['is_alt_1'] = hap_data[data.index.values,individual_idx_1]

        # Select nucleotides based on codes
        data['nt_0'] = np.where(data['is_alt_0'] == 0, data['a0'], data['a1'])
        data['nt_1'] = np.where(data['is_alt_1'] == 0, data['a0'], data['a1'])

        # Remove indels
        data = data[(data['a0'].str.len() == 1) & (data['a1'].str.len() == 1)]

        # Ensure sorted by position
        data.sort('position', inplace=True)

        # Add chromosome for full table
        data['chromosome'] = chromosome

        # Reformat output
        data = data.rename(columns={'a0':'ref', 'a1':'alt'})
        data = data[['chromosome', 'position', 'ref', 'alt', 'is_alt_0', 'is_alt_1', 'nt_0', 'nt_1']]

        sim_alleles.append(data)

    sim_alleles = pd.concat(sim_alleles, ignore_index=True)

    return sim_alleles
