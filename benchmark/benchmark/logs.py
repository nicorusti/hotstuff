from datetime import datetime
from glob import glob
from multiprocessing import Pool
from os.path import join
from re import findall, search
from statistics import mean
import json
from benchmark.utils import Print


class ParseError(Exception):
    pass


class LogParser:
    def __init__(self, clients, nodes, faults=0):
        inputs = [clients, nodes]
        assert all(isinstance(x, list) for x in inputs)
        assert all(isinstance(x, str) for y in inputs for x in y)
        assert all(x for x in inputs)
        self.faults = faults
        self.committee_size = len(nodes) + faults

        # Parse the clients logs.
        try:
            with Pool() as p:
                results = p.map(self._parse_clients, clients)
        except (ValueError, IndexError) as e:
            raise ParseError(f'Failed to parse client logs: {e}')
        self.size, self.rate, self.start, misses, self.sent_samples \
            = zip(*results)
        self.misses = sum(misses)

        # Parse the nodes logs.
        try:
            with Pool() as p:
                results = p.map(self._parse_nodes, nodes)
        except (ValueError, IndexError) as e:
            raise ParseError(f'Failed to parse node logs: {e}')
        proposals, commits, sizes, self.received_samples, timeouts, self.configs \
            = zip(*results)
        self.proposals = self._merge_results([x.items() for x in proposals])
        self.commits = self._merge_results([x.items() for x in commits])
        self.sizes = {
            k: v for x in sizes for k, v in x.items() if k in self.commits
        }
        self.timeouts = max(timeouts)

        # Check whether clients missed their target rate.
        if self.misses != 0:
            Print.warn(
                f'Clients missed their target rate {self.misses:,} time(s)'
            )

        # Check whether the nodes timed out.
        # Note that nodes are expected to time out once at the beginning.
        if self.timeouts > 1:
            Print.warn(f'Nodes timed out {self.timeouts:,} time(s)')

    def _merge_results(self, input):
        merged = {}
        for x in input:
            for k, v in x:
                if not k in merged or merged[k] > v:
                    merged[k] = v
        return merged

    def _parse_clients(self, log):
        if search(r'Error', log) is not None:
            raise ParseError('Client(s) panicked')

        size = int(search(r'Transactions size: (\d+)', log).group(1))
        rate = int(search(r'Transactions rate: (\d+)', log).group(1))

        tmp = search(r'\[(.*Z) .* Start ', log).group(1)
        start = self._to_posix(tmp)

        misses = len(findall(r'rate too high', log))

        tmp = findall(r'\[(.*Z) .* sample transaction', log)
        samples = [self._to_posix(x) for x in tmp]

        return size, rate, start, misses, samples

    def _parse_nodes(self, log):
        if search(r'panic', log) is not None:
            raise ParseError('Client(s) panicked')

        tmp = findall(r'\[(.*Z) .* Created B\d+\(([^ ]+)\)', log)
        tmp = [(d, self._to_posix(t)) for t, d in tmp]
        proposals = self._merge_results([tmp])

        tmp = findall(r'\[(.*Z) .* Committed B\d+\(([^ ]+)\)', log)
        tmp = [(d, self._to_posix(t)) for t, d in tmp]
        commits = self._merge_results([tmp])

        tmp = findall(r'Payload ([^ ]+) contains (\d+) B', log)
        sizes = {d: int(s) for d, s in tmp}

        tmp = findall(r'\[(.*Z) .* Payload ([^ ]+) contains (\d+) sample', log)
        samples = {d: (int(s), self._to_posix(t)) for t, d, s in tmp}

        tmp = findall(r'.* WARN .* Timeout', log)
        timeouts = len(tmp)

        configs = {
            'consensus': {
                'max_payload_size': int(
                    search(r'Consensus max payload size .* (\d+)', log).group(1)
                ),
                'min_block_delay': int(
                    search(r'Consensus min block delay .* (\d+)', log).group(1)
                ),
            },
            'mempool': {
                'max_payload_size': int(
                    search(r'Mempool max payload size .* (\d+)', log).group(1)
                ),
                'min_block_delay': int(
                    search(r'Mempool min block delay .* (\d+)', log).group(1)
                ),
            }
        }

        return proposals, commits, sizes, samples, timeouts, configs

    def _to_posix(self, string):
        x = datetime.fromisoformat(string.replace('Z', '+00:00'))
        return datetime.timestamp(x)

    def _consensus_throughput(self):
        if not self.commits:
            return 0, 0, 0
        start, end = min(self.proposals.values()), max(self.commits.values())
        duration = end - start
        bytes = sum(self.sizes.values())
        bps = bytes / duration
        tps = bps / self.size[0]
        return tps, bps, duration

    def _consensus_throughput_intervals(self,interval_len):
        start, end = min(self.proposals.values()), max(self.commits.values())
        tps_per_time = {}
        i=0
        for t in range(int(start), int(end), interval_len):
            relevant_blocks = {k:v for k, v in self.commits.items() if v>=t and v < t+interval_len}
            relevant_blocks_size = {k:s for k, s in self.sizes.items() if k in relevant_blocks}
            bytes=sum(relevant_blocks_size.values())
            bps = bytes / interval_len
            tps = bps / self.size[0]
            tps_per_time[i]=tps
            i=i+interval_len
            json.dump(tps_per_time, open('tps_intervals.log','w'))

    def _consensus_latency(self):
        latency = [c - self.proposals[d] for d, c in self.commits.items()]
        return mean(latency) if latency else 0

    def _end_to_end_throughput(self):
        if not self.commits:
            return 0, 0, 0
        start, end = min(self.start), max(self.commits.values())
        duration = end - start
        bytes = sum(self.sizes.values())
        bps = bytes / duration
        tps = bps / self.size[0]
        return tps, bps, duration

    def _end_to_end_latency(self):
        latency = []
        for sent, data in zip(self.sent_samples, self.received_samples):
            sent.sort()

            ordered = sorted(list(data.items()), key=lambda x: x[1][1])
            commit = []
            for digest, (occurrences, _) in ordered:
                tmp = self.commits.get(digest)
                commit += [tmp] * occurrences

            latency += [x - y for x, y in zip(commit, sent) if x is not None]
        return mean(latency) if latency else 0

    def result(self):
        consensus_latency = self._consensus_latency() * 1000
        consensus_tps, consensus_bps, _ = self._consensus_throughput()
        self._consensus_throughput_intervals(10)
        end_to_end_tps, end_to_end_bps, duration = self._end_to_end_throughput()
        end_to_end_latency = self._end_to_end_latency() * 1000

        consensus_max_payload_size = self.configs[0]['consensus']['max_payload_size']
        consensus_min_block_delay = self.configs[0]['consensus']['min_block_delay']
        mempool_max_payload_size = self.configs[0]['mempool']['max_payload_size']
        mempool_min_block_delay = self.configs[0]['mempool']['min_block_delay']

        return (
            '\n'
            '-----------------------------------------\n'
            ' SUMMARY:\n'
            '-----------------------------------------\n'
            ' + CONFIG:\n'
            f' Committee size: {self.committee_size} nodes\n'
            f' Input rate: {sum(self.rate):,} tx/s\n'
            f' Transaction size: {self.size[0]:,} B\n'
            f' Faults: {self.faults} nodes\n'
            f' Execution time: {round(duration):,} s\n'
            '\n'
            f' Consensus max payloads size: {consensus_max_payload_size:,} B\n'
            f' Consensus min block delay: {consensus_min_block_delay:,} ms\n'
            f' Mempool max payloads size: {mempool_max_payload_size:,} B\n'
            f' Mempool min block delay: {mempool_min_block_delay:,} ms\n'
            '\n'
            ' + RESULTS:\n'
            f' Consensus TPS: {round(consensus_tps):,} tx/s\n'
            f' Consensus BPS: {round(consensus_bps):,} B/s\n'
            f' Consensus latency: {round(consensus_latency):,} ms\n'
            '\n'
            f' End-to-end TPS: {round(end_to_end_tps):,} tx/s\n'
            f' End-to-end BPS: {round(end_to_end_bps):,} B/s\n'
            f' End-to-end latency: {round(end_to_end_latency):,} ms\n'
            '-----------------------------------------\n'
        )

    def print(self, filename):
        assert isinstance(filename, str)
        with open(filename, 'a') as f:
            f.write(self.result())

    @classmethod
    def process(cls, directory, faults=0):
        assert isinstance(directory, str)

        clients = []
        for filename in glob(join(directory, 'client-*.log')):
            with open(filename, 'r') as f:
                clients += [f.read()]
        nodes = []
        for filename in glob(join(directory, 'node-*.log')):
            with open(filename, 'r') as f:
                nodes += [f.read()]

        return cls(clients, nodes, faults=faults)
