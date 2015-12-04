import numpy as np
import pandas as pd
import statsmodels.api as sm




def sample_gc(gc_samples_filename, seqdata_filename, fragment_length, config):

    chromosomes = config['chromosomes']
    num_samples = config['sample_gc_num_positions']
    position_offset = config['sample_gc_offset']
    genome_fai = config['genome_fai']
    genome_fasta = config['genome_fasta']
    mappability_filename = config['mappability_filename']

    fragment_length = int(fragment_length)
    gc_window = fragment_length - 2 * position_offset

    chrom_info = pd.DataFrame({'chrom_length':remixt.utils.read_chromosome_lengths(genome_fai)})
    chrom_info = chrom_info.reindex(chromosomes)
    chrom_info['chrom_end'] = chrom_info['chrom_length'].cumsum()
    chrom_info['chrom_start'] = chrom_info['chrom_end'] - chrom_info['chrom_length']

    # Sample random genomic positions from concatenated genome
    genome_length = chrom_info['chrom_length'].sum()
    sample_pos = np.sort(np.random.randint(0, genome_length, num_samples))

    # Calculate mappability for each sample
    sample_mappability = np.zeros(sample_pos.shape)

    # Iterate large mappability file
    mappability_iter = pd.read_csv(mappability_filename, sep='\t', header=None, iterator=True, chunksize=10000,
        converters={'chromosome':str}, names=['chromosome', 'start', 'end', 'score'])
    for mappability in mappability_iter:

        # Filter extraneous chromosomes
        mappability = mappability[mappability['chromosome'].isin(chromosomes)]

        # Perfect mappability only
        mappability = mappability[mappability['score'] == 1]

        # Add chromosome start end and calculate start/end in concatenated genome
        mappability = mappability.merge(chrom_info[['chrom_start']], left_on='chromosome', right_index=True)
        mappability['start'] += mappability['chrom_start']
        mappability['end'] += mappability['chrom_start']

        # Add mappability for current iteration
        sample_mappability += remixt.segalg.overlapping_counts(sample_pos, mappability[['start', 'end']].values)

    # Filter unmappable positions
    sample_pos = sample_pos[sample_mappability > 0]

    # Calculate GC for each position
    sample_gc_count = np.zeros(sample_pos.shape)
    for chrom_id, sequence in remixt.utils.read_sequences(genome_fasta):

        # Ignore extraneous chromosomes
        if chrom_id not in chromosomes:
            continue

        # Start and end of current chromosome in concatenated genome
        chrom_start, chrom_end = chrom_info.loc[chrom_id, ['chrom_start', 'chrom_end']].values

        # Calculate gc count within sliding window
        sequence = np.array(list(sequence.upper()))
        gc = ((sequence == 'G') | (sequence == 'C'))
        gc_count = gc.cumsum()
        gc_count[gc_window:] = gc_count[gc_window:] - gc.cumsum()[:-gc_window]

        # Append nan for fragments too close to the end of the chromosome
        gc_count = np.concatenate([gc_count, np.ones(fragment_length) * np.nan])

        # Calculate filter of positions in this chromosome
        chrom_sample_idx = (sample_pos >= chrom_start) & (sample_pos < chrom_end)

        # Calculate last position in window
        chrom_window_end = sample_pos[chrom_sample_idx] - chrom_start + fragment_length - position_offset - 1

        # Add the gc count for filtered positions
        sample_gc_count[chrom_sample_idx] += gc_count[chrom_window_end]

    # Filter nan gc count values
    sample_pos = sample_pos[~np.isnan(sample_gc_count)]
    sample_gc_count = sample_gc_count[~np.isnan(sample_gc_count)]

    sample_gc_percent = sample_gc_count / float(gc_window)

    # Count number of reads at each position
    sample_read_count = np.zeros(sample_pos.shape, dtype=int)
    for chrom_id in remixt.seqdataio.read_chromosomes(seqdata_filename):

        # Ignore extraneous chromosomes
        if chrom_id not in chromosomes:
            continue

        for chrom_reads in remixt.seqdataio.read_read_data(seqdata_filename, chromosome=chrom_id, num_rows=1000000):

            # Calculate read start in concatenated genome
            chrom_reads['start'] += chrom_info.loc[chrom_id, 'chrom_start']

            # Add reads at each start
            sample_read_count += (
                chrom_reads
                .groupby('start')['end']
                .count()
                .reindex(sample_pos)
                .fillna(0)
                .astype(int)
                .values
            )

    # Calculate position in non-concatenated genome
    sample_chrom_idx = np.searchsorted(chrom_info['chrom_end'].values, sample_pos, side='right')
    sample_chrom = chrom_info.index.values[sample_chrom_idx]
    sample_chrom_pos = sample_pos - chrom_info['chrom_start'].values[sample_chrom_idx]

    # Output chromosome, position, gc percent, read count
    gc_sample_data = pd.DataFrame({
        'chromosome':sample_chrom,
        'position':sample_chrom_pos,
        'gc_percent':sample_gc_percent,
        'read_count':sample_read_count,
    })
    gc_sample_data = gc_sample_data[[
        'chromosome',
        'position',
        'gc_percent',
        'read_count'
    ]]

    gc_sample_data.to_csv(gc_samples_filename, sep='\t', header=False, index=False)


def gc_lowess(gc_samples_filename, gc_dist_filename, gc_table_filename, gc_resolution=100):

    gc_samples = pd.read_csv(gc_samples_filename, sep='\t', names=['chromosome', 'position', 'gc', 'count'])

    gc_samples['gc_bin'] = np.round(gc_samples['gc'] * gc_resolution)

    gc_binned = gc_samples.groupby('gc_bin')['count'] \
                          .agg({'sum':np.sum, 'len':len, 'mean':np.mean}) \
                          .reindex(xrange(gc_resolution+1)) \
                          .fillna(0) \
                          .reset_index() \
                          .rename(columns={'index':'gc_bin'}) \
                          .astype(float)

    gc_binned['smoothed'] = sm.nonparametric.lowess(gc_binned['mean'].values, gc_binned['gc_bin'].values, frac=0.2).T[1]
    assert not gc_binned['smoothed'].isnull().any()

    rescale = 1. / gc_binned['smoothed'].max()

    gc_binned['mean'] = gc_binned['mean'] * rescale
    gc_binned['smoothed'] = gc_binned['smoothed'] * rescale

    gc_binned.to_csv(gc_table_filename, sep='\t', index=False)

    gc_binned[['smoothed']].to_csv(gc_dist_filename, sep='\t', index=False, header=False)


def read_mappability_indicator(mappability_filename, chromosome, chromosome_length):
    """ Read a mappability wig file into a mappability vector
    """
    mappability_table = pd.read_csv(mappability_filename, sep='\t', header=None,
        converters={'chromosome':str}, names=['chromosome', 'start', 'end', 'score'])

    mappability = np.zeros(chromosome_length, dtype=np.uint8)

    for start, end, value in mappability_table.loc[mappability_table['chromosome'] == chromosome, ['start', 'end', 'score']].values:
        mappability[start:end] = value

    return mappability


def read_gc_cumsum(genome_fasta, chromosome):
    """ Read a chromosome sequence and create GC cumulative sum

    TODO: optimize using genome fasta index
    """
    for c, s in remixt.utils.read_sequences(genome_fasta):
        if c == chromosome:
            s = np.array(list(s.upper()), dtype=np.character)
            gc_indicator = ((s == 'G') | (s == 'C')) * 1

    gc_cumsum = gc_indicator.cumsum()

    return gc_cumsum


class GCCurve(object):
    """ Piecewise linear GC probability curve
    """
    def read(self, gc_lowess_filename):
        """ Read from a text file
        """
        with open(gc_lowess_filename, 'r') as f:
            self.gc_lowess = np.array(f.readlines(), dtype=float)
        self.gc_lowess /= self.gc_lowess.sum()

    def predict(self, x):
        """ Calculate GC probability from percent
        """
        idx = np.clip(int(x * float(len(self.gc_lowess) - 1)), 0, len(self.gc_lowess) - 1)
        return max(self.gc_lowess[idx], 0.0)

    def table(self, l):
        """ Tabulate GC probabilities for a specific fragment length
        """
        return np.array([self.predict(float(x)/float(l)) for x in xrange(0, l + 1)])


def gc_map_bias(segment_filename, fragment_mean, fragment_stddev, read_length, gc_lowess_filename, bias_filename, config):
    """ Calculate per segment GC and mappability biases
    """
    segments = pd.read_csv(segment_filename, sep='\t', converters={'chromosome':str})

    biases = calculate_gc_map_bias(segments, fragment_mean, fragment_stddev, read_length, gc_lowess_filename, config)

    biases.to_csv(bias_filename, sep='\t', index=False)


def calculate_gc_map_bias(segments, fragment_mean, fragment_stddev, read_length, gc_lowess_filename, config):
    """ Calculate per segment GC and mappability biases
    """
    position_offset = config['sample_gc_offset']
    genome_fai = config['genome_fai']
    genome_fasta = config['genome_fasta']
    mappability_filename = config['mappability_filename']

    gc_dist = GCCurve()
    gc_dist.read(gc_lowess_filename)

    fragment_dist = scipy.stats.norm(fragment_mean, fragment_stddev)

    fragment_min = int(fragment_dist.ppf(0.01) - 1.)
    fragment_max = int(fragment_dist.ppf(0.99) + 1.)

    for chromosome, chrom_seg in segments.groupby('chromosome'):

        chromosome_length = chrom_seg['end'].max()
        mappability = read_mappability_indicator(mappability_filename, chromosome, chromosome_length)
        gc_cumsum = read_gc_cumsum(genome_fasta, chromosome)

        for idx, (start, end) in segments[['start', 'end']].iterrows():
            segments.loc[idx, 'bias'] = calculate_segment_gc_map_bias(gc_cumsum[start:end], mappability[start:end],
                gc_dist, fragment_dist, fragment_min, fragment_max, position_offset, read_length)

    return segments


def calculate_segment_gc_map_bias(gc_cumsum, mappability, gc_dist, fragment_dist, fragment_min, fragment_max, position_offset, read_length):
    """ Calculate GC/mappability bias
    """
    bias = 0.

    for fragment_length in xrange(fragment_min, fragment_max+1):

        # Calculate total GC
        gc_sum = gc_cumsum[fragment_length-position_offset:-position_offset] - gc_cumsum[position_offset:-fragment_length+position_offset]
        gc_length = fragment_length - 2*position_offset
        
        # Create a table mapping total GC to probability
        gc_table = gc_dist.table(gc_length)

        # Calculate per position GC probability
        gc_prob = gc_table[gc_sum]

        # Calculate mappabilities
        mate_position = fragment_length - read_length
        map_prob = mappability[:-fragment_length] * mappability[mate_position:-read_length]

        # Calculate fragment length prob
        len_prob = fragment_dist.pdf(fragment_length)
        
        # Calculate per position probability
        prob = gc_prob * map_prob * len_prob
        
        bias += prob.sum()

    return bias

