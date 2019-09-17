import itertools as it
from multiprocessing import Pool
from multiprocessing import cpu_count

import pandas as pd
import numpy as np
from scipy.stats import fisher_exact
from scipy.stats import binom
import pysam
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord

from .mapped_reads import MappedReads


def grouper(iterable, n, fillvalue=None):
    args = [iter(iterable)] * n
    return it.zip_longest(*args, fillvalue=fillvalue)


def partial_covariation_test(arguments):
    pysam_path, index_path, pairs, i = arguments
    pairs, pairs_for_max, pairs_for_min = it.tee(
        it.filterfalse(lambda x: x is None, pairs),
        3
    )
    print('   ...processing block %d...' % i)
    pair_min = min([min(pair[0], pair[1]) for pair in pairs_for_min])
    pair_max = max([max(pair[0], pair[1]) for pair in pairs_for_max])
    mr = MappedReads(pysam_path, index_filename=index_path)
    fasta_window = mr.sam_window_to_fasta(pair_min, pair_max+1)
    results = []
    for col_i, col_j in pairs:
        idx_i = col_i - pair_min
        idx_j = col_j - pair_min
        i_char_counts = pd.Series(
            fasta_window[:, idx_i]
        ).value_counts().drop('~', errors='ignore')
        i_chars = i_char_counts.index[i_char_counts > 0]

        j_char_counts = pd.Series(
            fasta_window[:, idx_j]
        ).value_counts().drop('~', errors='ignore')
        j_chars = j_char_counts.index[j_char_counts > 0]

        content_i = fasta_window[:, idx_i] != '~'
        content_j = fasta_window[:, idx_j] != '~'
        valid = content_i & content_j
        if valid.sum() == 0:
            continue
        for i_char, j_char in it.product(i_chars, j_chars):
            equals_i = fasta_window[valid, idx_i] == i_char
            equals_j = fasta_window[valid, idx_j] == j_char
            X_11 = (equals_i & equals_j).sum()
            X_21 = (~equals_i & equals_j).sum()
            X_12 = (equals_i & ~equals_j).sum()
            X_22 = (~equals_i & ~equals_j).sum()

            table = [
                [X_11, X_12],
                [X_21, X_22]
            ]
            _, p_value = fisher_exact(table)
            results.append((
                col_i, col_j, i_char, j_char,
                X_11, X_12, X_21, X_22, p_value
            ))
    mr.close()
    print('   ...done block %d!' % i)
    return pd.DataFrame(
        results, columns=(
            'col_i', 'col_j', 'i_char', 'j_char',
            'x_11', 'x_12', 'x_21', 'x_22', 'p_value'
        )
    )


class ErrorCorrection:
    def __init__(
            self, pysam_alignment, all_cv_tests=None, error_threshold=1e-3,
            end_correction=None
            ):
        self.pysam_alignment = pysam_alignment
        self.reference_length = pysam_alignment.header['SQ'][0]['LN']
        self.number_of_reads = 0
        for read in pysam_alignment.fetch():
            self.number_of_reads += 1

        if all_cv_tests:
            self.all_cv_tests = pd.read_csv(all_cv_tests)
        else:
            self.all_cv_tests = None
        self.covarying_sites = None
        self.pairs = None
        self.nucleotide_counts = None
        self.error_threshold = error_threshold
        self.end_correction = end_correction or 0

    @staticmethod
    def read_count_data(read):
        sequence_length = np.array([
            cigar_tuple[1]
            for cigar_tuple in read.cigartuples
            if cigar_tuple[0] != 1
        ]).sum()
        first_position = read.reference_start
        last_position = first_position + sequence_length
        positions = np.arange(first_position, last_position)
        segments = []
        number_of_cigar_tuples = len(read.cigartuples)
        unaligned_sequence = read.query_alignment_sequence
        position = 0
        for i, cigar_tuple in enumerate(read.cigartuples):
            action = cigar_tuple[0]
            stride = cigar_tuple[1]
            match = action == 0
            insertion = action == 1
            deletion = action == 2
            if match:
                segments.append(
                    unaligned_sequence[position: position + stride]
                    )
                position += stride
            elif insertion:
                position += stride
            elif deletion:
                if len(segments) > 0 and i < number_of_cigar_tuples:
                    segments.append(stride * '-')
        sequence = np.concatenate([list(segment) for segment in segments])
        return sequence, positions

    @staticmethod
    def supplementary_info(row):
        result = row.loc[['A', 'C', 'G', 'T']] \
            .sort_values(ascending=False)
        result.index = ['c1', 'c2', 'c3', 'c4']
        return result.append(pd.Series(
            result.values/row['coverage'] if row['coverage'] else 0,
            index=['f1', 'f2', 'f3', 'f4']
        ))

    def get_nucleotide_counts(self):
        if self.nucleotide_counts is not None:
            return self.nucleotide_counts
        print('Calculating nucleotide counts...')
        characters = ['A', 'C', 'G', 'T', '-']
        counts = np.zeros((self.reference_length, 5))
        for read in self.pysam_alignment.fetch():
            sequence, positions = self.read_count_data(read)
            for character_index, character in enumerate(characters):
                rows = positions[sequence == character]
                counts[rows, character_index] += 1

        df = pd.DataFrame(counts, columns=characters)
        def zeros(character): return (df[character] == 0).astype(np.int)
        zero_cols = zeros('A') + zeros('C') + zeros('G') + zeros('T')
        df['interesting'] = zero_cols < 3
        df['nucleotide_max'] = df[['A', 'C', 'G', 'T']].max(axis=1)
        df['coverage'] = df[['A', 'C', 'G', 'T']].sum(axis=1)
        df['consensus'] = '-'
        for character in characters[:-1]:
            consensus_agreement = df['nucleotide_max'] == df[character]
            df.loc[consensus_agreement, 'consensus'] = character
        df = pd.concat([df, df.apply(self.supplementary_info, axis=1)], axis=1)
        self.nucleotide_counts = df
        return df

    def consensus(self):
        consensus_sequence = Seq(''.join(self.nucleotide_counts['consensus']))
        record = SeqRecord(consensus_sequence, id='consensus', description='')
        return record

    def get_pairs(self):
        if self.pairs:
            return self.pairs
        counts = self.get_nucleotide_counts()
        interesting = counts.index[counts.interesting]
        max_read_length = max([
            read.infer_query_length()
            for read in self.pysam_alignment.fetch()
        ])
        pairs = list(filter(
            lambda pair: pair[1] - pair[0] <= max_read_length,
            it.combinations(interesting, 2)
        ))
        self.pairs = pairs
        return pairs

    def full_covariation_test(
            self, threshold=20, stride=10000, ncpu=None, block_size=250
            ):
        if self.covarying_sites is not None:
            return self.covarying_sites
        pairs = self.get_pairs()
        filename = self.pysam_alignment.filename
        index_filename = self.pysam_alignment.index_filename
        arguments = [
            (filename, index_filename, group, i)
            for i, group in enumerate(grouper(pairs, block_size))
        ]
        n_pairs = len(pairs)
        n_blocks = len(arguments)
        message = 'Processing %d blocks of %d pairs with %d processes...'
        ncpu = ncpu or cpu_count()
        print(message % (n_blocks, n_pairs, ncpu))
        pool = Pool(processes=ncpu)
        result_dfs = pool.map(partial_covariation_test, arguments)
        pool.close()
        self.all_cv_tests = pd.concat(result_dfs).sort_values(by='p_value')
        print('...done!')

    def multiple_testing_correction(self, fdr=.001):
        print('Performing multiple testing correction...')
        m = len(self.all_cv_tests)
        bh_corrected = self.all_cv_tests['p_value'] <= fdr*np.arange(1, m+1)/m
        self.all_cv_tests['bh'] = bh_corrected
        cutoff = (1-self.all_cv_tests['bh']).to_numpy().nonzero()[0][0]
        covarying_sites = np.unique(
            np.concatenate([
                self.all_cv_tests['col_i'].iloc[:cutoff],
                self.all_cv_tests['col_j'].iloc[:cutoff]
            ])
        )
        covarying_sites.sort()
        after_head_correction = covarying_sites > self.end_correction
        tail_cutoff = self.reference_length - self.end_correction
        before_tail_correction = covarying_sites < tail_cutoff
        desired_sites = after_head_correction & before_tail_correction
        covarying_sites = covarying_sites[desired_sites]
        self.covarying_sites = covarying_sites
        self.nucleotide_counts.loc[:, 'covarying'] = False
        self.nucleotide_counts.loc[covarying_sites, 'covarying'] = True
        return covarying_sites

    def get_covarying_errors(self):
        nucleotide_counts = self.get_nucleotide_counts()
        summary = nucleotide_counts.loc[
            ~nucleotide_counts.covarying,
            ['nucleotide_max', 'coverage']
        ].sum()
        total_coverage = summary['coverage']
        total_consensus = summary['nucleotide_max']
        error_rate = np.abs(total_coverage - total_consensus) / total_consensus
        nucleotide_counts.loc[:, 'n_error'] = \
            nucleotide_counts.loc[:, 'coverage'].apply(
                lambda count: binom.ppf(
                    1-self.error_threshold, count, error_rate
                    )
            )
        nucleotide_counts.loc[:, 'site'] = nucleotide_counts.index
        nucleotide_counts.loc[:, 'covarying'] = False
        nucleotide_counts.loc[self.covarying_sites, 'covarying'] = True
        site_counts = nucleotide_counts.loc[
            nucleotide_counts['covarying'], :
        ].melt(
            id_vars=['n_error', 'site'], value_vars=['A', 'C', 'G', 'T']
        )
        covarying_values = site_counts['value']
        covarying_counts = site_counts['n_error']
        significant = (covarying_values <= covarying_counts) & \
            (covarying_values > 0)
        covarying_errors = site_counts.loc[significant, :] \
            .sort_values(by='site') \
            .reset_index(drop=True)
        self.covarying_errors = covarying_errors
        return covarying_errors

    def get_covarying_correction(self, read, discrepancies):
        for discrepancy in discrepancies:
            pass

    def get_all_covarying_corrections(self):
        covarying_errors = self.get_covarying_errors()
        self.read_information = self.mapped_reads.read_reference_start_and_end(
            self.covarying_sites
        )
        corrections = {}
        for i, read in enumerate(self.mapped_reads.reads):
            ce_start = (
                covarying_errors['site'] >= read.reference_start
                ).idxmax()
            ce_end = (
                covarying_errors['site'] >= read.reference_end
                ).idxmax()-1
            sequence = read.to_fasta()
            unshifted_indices = covarying_errors.loc[ce_start:ce_end, 'site']
            shifted_indices = unshifted_indices - read.reference_start
            discrepancies = []
            for shifted_index in shifted_indices:
                # if sequence[shifted_index] =
                pass
            cv_sequence = sequence[shifted_indices]
            cv_errors = covarying_errors.loc[ce_start:ce_end, 'variable']
            if (cv_sequence == cv_errors).any():
                read_length = len(cv_sequence)
                difference = cv_sequence != cv_errors
                discrepancies = np.arange(read_length)[difference]
                corrections[read.query_name] = self.get_covarying_correction(
                    read, i, discrepancies
                )

    def corrected_reads(self, **kwargs):
        end_correction = self.end_correction
        nucleotide_counts = self.get_nucleotide_counts()
        if not self.covarying_sites:
            self.full_covariation_test()
            covarying_sites = self.multiple_testing_correction()
        else:
            covarying_sites = self.covarying_sites
        if end_correction:
            tail_cutoff = self.reference_length - end_correction

        for read in self.pysam_alignment.fetch():
            sequence, _ = self.read_count_data(read)
            intraread_covarying_sites = covarying_sites[
                (covarying_sites >= read.reference_start) &
                (covarying_sites < read.reference_end)
            ]
            mask = np.ones(len(sequence), np.bool)
            mask[intraread_covarying_sites - read.reference_start] = False
            local_consensus = nucleotide_counts.consensus[
                read.reference_start: read.reference_end
            ]
            sequence[mask] = local_consensus[mask]

            if end_correction:
                if read.reference_start < end_correction:
                    query_index = end_correction - read.reference_start
                    query_correction = nucleotide_counts.consensus[
                        read.reference_start: end_correction
                    ]
                    sequence[0: query_index] = query_correction
                if read.reference_end > tail_cutoff:
                    correction_length = read.reference_end - tail_cutoff
                    query_correction = nucleotide_counts.consensus[
                        tail_cutoff: tail_cutoff + correction_length
                    ]
                    sequence[-correction_length:] = query_correction

            corrected_read = pysam.AlignedSegment()
            corrected_read.query_name = read.query_name
            corrected_read.query_sequence = ''.join(sequence)
            corrected_read.flag = read.flag
            corrected_read.reference_id = 0
            corrected_read.reference_start = read.reference_start
            corrected_read.mapping_quality = read.mapping_quality
            corrected_read.cigar = [(0, len(sequence))]
            corrected_read.next_reference_id = read.next_reference_id
            corrected_read.next_reference_start = read.next_reference_start
            corrected_read.template_length = read.template_length
            corrected_read.query_qualities = pysam.qualitystring_to_array(
                len(sequence) * '<'
            )
            corrected_read.tags = read.tags
            yield corrected_read

    def write_corrected_reads(self, output_bam_filename, end_correction=None):
        output_bam = pysam.AlignmentFile(
            output_bam_filename, 'wb', header=self.pysam_alignment.header
        )
        for read in self.corrected_reads(end_correction=end_correction):
            output_bam.write(read)
        output_bam.close()

    def simple_thresholding(self, threshold=.01):
        self.get_nucleotide_counts()
        above_threshold = (
            self.nucleotide_counts
            .loc[:, ['f1', 'f2', 'f3', 'f4']] > threshold
        ).sum(axis=1)
        all_integers = np.arange(0, len(above_threshold))
        covarying_sites = all_integers[above_threshold > 1]
        number_of_sites = len(self.nucleotide_counts)
        after_head_correction = covarying_sites > self.end_correction
        final_site = number_of_sites - self.end_correction
        before_tail_correction = covarying_sites < final_site
        desired = after_head_correction & before_tail_correction
        self.covarying_sites = covarying_sites[desired]

    def kmers_in_reads(self, k=4):
        kmer_dict = {}
        for covarying_site in self.covarying_sites:
            kmer_dict[covarying_site] = {}
        print('Determining covarying %d-mers in reads...' % k)
        for read_index, read in enumerate(self.pysam_alignment.fetch()):
            after_start = self.covarying_sites >= read.reference_start
            before_end = self.covarying_sites < read.reference_end
            desired = after_start & before_end
            inter_read_cvs = self.covarying_sites[desired]
            characters_at_cvs = [
                read.query[pair[0]]
                for pair in read.get_aligned_pairs(matches_only=True)
                if pair[1] in inter_read_cvs
            ]
            for i in range(len(inter_read_cvs)-k):
                kmer = ''.join(characters_at_cvs[i:i+k])
                if kmer in kmer_dict[inter_read_cvs[i]]:
                    kmer_dict[inter_read_cvs[i]][kmer].append(read.query_name)
                else:
                    kmer_dict[inter_read_cvs[i]][kmer] = [read.query_name]
            if read_index % 10000 == 0:
                print(' ...finished read %d' % read_index, end='', flush=True)
        print('...done!')
        return kmer_dict

    def filtered_reads(self, k=4, cutoff=20, minimum_length=100):
        skip_dict = {}
        self.simple_thresholding()
        kmer_dict = self.kmers_in_reads(k)
        for covarying_site, single_kmer_dict in kmer_dict.items():
            bad_reads = it.chain.from_iterable([
                value
                for key, value in single_kmer_dict.items()
                if len(value) < cutoff
            ])
            for bad_read in bad_reads:
                skip_dict[bad_read] = True
        for read in self.pysam_alignment.fetch():
            should_not_skip = read.query_name not in skip_dict
            long_enough = read.query_length > minimum_length
            valid = should_not_skip and long_enough
            if valid:
                yield read

    def write_filtered_reads(self, output_bam_path):
        output_bam = pysam.AlignmentFile(
            output_bam_path, 'wb', header=self.pysam_alignment.header
        )
        for read in self.filtered_reads():
            output_bam.write(read)
        output_bam.close()

    def __del__(self):
        self.pysam_alignment.close()
