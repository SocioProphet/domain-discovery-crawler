from collections import Counter
import gzip
import json
import logging
import math
import random
import struct
import time
from typing import Optional, List, Tuple, Union, Dict
from zlib import crc32

from deepdeep.utils import softmax
import lib.smaz as smaz
import numpy as np
from redis.client import StrictRedis
from scrapy import Request
from scrapy_redis.queue import Base

from .signals import queues_changed
from .utils import warn_if_slower, cacheforawhile, get_domain


logger = logging.getLogger(__name__)


# Note about race conditions: there are several workers executing this code, but
# - Redis itself is single-threaded
# - Only one worker should be crawling given domain, unless workers enter/leave


class BaseRequestQueue(Base):
    """ Request queue where each domain has a separate queue,
    and each domain is crawled only by one worker to be polite.

    QUEUE_CACHE_TIME setting determines the time queues are cached for,
    when workers do not change (stale cache only leads to missing new domains
    for a while, so it's safe to set it to higher values).
    """
    def __init__(self, *args, slots_mock=None, skip_cache=False, **kwargs):
        super().__init__(*args, **kwargs)
        assert isinstance(self.server, StrictRedis)
        logging.info('Init {} queue with key {}'.format(type(self), self.key))
        self.len_key = self.fkey('len')  # int
        self.queues_key = self.fkey('queues')  # sorted set
        self.relevant_queues_key = self.fkey('relevant-queues')  # sorted set
        self.did_restrict_key = self.fkey('did-restrict-domains')  # bool
        # set of domains with login form found
        self.has_login_form_key = self.fkey('login-form-domains')
        # hash with domain as key and json-encoded credentials as value
        self.login_credentials_key = self.fkey('login-credentials')
        self.workers_key = self.fkey('workers')  # set
        self.worker_id_key = self.fkey('worker-id')  # int
        self.worker_id = self.server.incr(self.worker_id_key)
        self.alive_timeout = 120  # seconds
        self.im_alive()
        self.n_pops = 0
        self.stat_each = 1000  # requests
        self.slots_mock = slots_mock
        self.skip_cache = skip_cache
        settings = self.spider.settings
        self.max_domains = settings.getint('QUEUE_MAX_DOMAINS')
        if self.max_domains:
            logging.warning(
                'QUEUE_MAX_DOMAINS has a bug: '
                'domains which queue becomes empty during crawling '
                'can disappear from the domain queue.')
        self.max_relevant_domains = \
            settings.getint('QUEUE_MAX_RELEVANT_DOMAINS')
        self.set_spider_domain_limit()
        self.start_time = time.time()
        self.restrict_delay = settings.getint('RESTRICT_DELAY', 3600)  # seconds

    def __len__(self):
        return int(self.server.get(self.len_key) or '0')

    def push(self, request: Request) -> bool:
        """ Push request to queue. Return False if it has not been pushed.
        """
        queue_key = self.url_queue_key(request.url)
        if (self.max_domains and
                self.server.zcard(self.queues_key) >= self.max_domains and
                self.server.zrank(self.queues_key, queue_key) is None):
            # Do not add new queue, limit has been reached
            return False
        if (self.did_restrict_domains and
                self.server.zrank(self.relevant_queues_key, queue_key) is None):
            # Such requests could come from the time we selected
            # relevant domains: some requests were in fly or in batches.
            return False
        data = self._encode_request(request)
        score = -min(request.priority,
                     self.spider.settings.getfloat('DD_MAX_SCORE', np.inf))
        added = self.server.zadd(queue_key, score, data)
        if added:
            self.server.incr(self.len_key)
        top = self.server.zrange(queue_key, 0, 0, withscores=True)
        if top:
            (_, queue_score), = top
        else:  # a race during domain re-balancing: do not care about score much
            logger.warning('Placing a possibly incorrect queue score')
            queue_score = score
        self.add_queue(queue_key, queue_score)
        return True

    def pop(self, timeout=0) -> Optional[Request]:
        self.update_queue_stats()
        queue_key = self.select_queue_key()
        if queue_key:
            results = self.pop_from_queue(queue_key, 1)
            if results:
                return results[0]

    def update_queue_stats(self, update_domains=True):
        crawler = self.spider.crawler
        stats = crawler.stats
        stats.set_value('dd_crawler/queue/urls', len(self))
        if update_domains:
            n_domains_key = 'dd_crawler/queue/domains'
            prev_n_domains = stats.get_value(n_domains_key)
            n_domains = self.server.zcard(self.queues_key)
            if prev_n_domains != n_domains:
                # In theory it can happen that domains changed but count stayed
                # the same due to a race conditions with other workers.
                # In practice this is not an issue.
                crawler.signals.send_catch_log_deferred(
                    signal=queues_changed, queue=self)
                stats.set_value(n_domains_key, n_domains)
            if self.max_relevant_domains:
                stats.set_value('dd_crawler/queue/relevant_domains',
                                self.server.zcard(self.relevant_queues_key))

    def clear(self):
        logging.info('Clearing all keys for {}'.format(self.key))
        keys = {self.len_key, self.queues_key, self.relevant_queues_key,
                self.did_restrict_key, self.workers_key, self.worker_id_key}
        keys.update(self.get_workers())
        keys.update(self.get_queues())
        self.server.delete(*keys)
        super().clear()

    def get_queues(self, withscores=False
                   ) -> Union[List[bytes], List[Tuple[bytes, float]]]:
        return self.server.zrange(self.queues_key, 0, -1, withscores=withscores)

    def try_to_restrict_domains(self):
        if (self.restrict_domanis
            and not self.did_restrict_domains
            and time.time() - self.start_time > self.restrict_delay
            and self.server.zcard(self.relevant_queues_key) >=
                self.max_relevant_domains):
            selected_relevant = set(self.server.zrange(
                self.relevant_queues_key, 0, self.max_relevant_domains - 1))
            irrelevant = (set(self.server.zrange(self.queues_key, 0, -1)) -
                          selected_relevant)
            logger.info(
                'Removing {:,} irrelevant domains. {:,} relevant domains left'
                .format(len(irrelevant), len(selected_relevant)))
            self.server.zrem(self.queues_key, *irrelevant)
            self.server.set(self.did_restrict_key, b'1')

    def set_spider_domain_limit(self):
        """ Set domain_limit attribute on the spider: it is read by middlewares
        that limit spider to existing domains.
        """
        if self.did_restrict_domains:
            self.spider.domain_limit = True

    @property
    def did_restrict_domains(self) -> bool:
        """ Relevant domains have already been selected.
        """
        return self.restrict_domanis and self.server.get(self.did_restrict_key)

    def page_is_relevant(self, url: str, score: float):
        """ Mark page domain as relevant, if max_relevant_domains is set.
        """
        if self.max_relevant_domains:
            queue_key = self.url_queue_key(url)
            self.server.zincrby(self.relevant_queues_key, queue_key, -score**2)

    def get_workers(self) -> List[bytes]:
        return self.server.smembers(self.workers_key)

    @warn_if_slower(0.1, logger)
    def select_queue_key(self) -> Optional[bytes]:
        """ Select which queue (domain) to use next.
        """
        idx, n_idx = self.discover()
        self.get_my_queues(idx, n_idx)  # This is a caching trick:
        # the trick above is needed because get_available_queues calls
        # get_my_queues, which is also cached, but we want independent
        # runtime estimates for them. So we cache get_my_queues here, and
        # runtime of get_available_queues does not include get_my_queues.
        # TODO - track this in cacheforawhile
        queue = self.select_best_queue(idx, n_idx)
        if queue:
            if self.server.zcard(queue):
                return queue
            else:
                self.remove_queue(queue)

    def select_best_queue(self, idx: int, n_idx: int) -> Optional[bytes]:
        """ Select queue to crawl from, taking free slots into account.
        """
        available_queues, scores = self.get_available_queues(idx, n_idx)
        if available_queues:
            return random.choice(available_queues)

    @cacheforawhile
    def get_available_queues(self, idx: int, n_idx: int)\
            -> Tuple[List[bytes], np.ndarray]:
        """ Return all queues with free slots (or just all) and their weights.
        """
        all_queues, all_scores = self.get_my_queues(idx, n_idx)
        slots = self.get_slots()
        available_queues, scores = [], []
        for q, s in zip(all_queues, all_scores):
            domain = self.queue_key_domain(q)
            if domain not in slots or slots[domain].free_transfer_slots():
                available_queues.append(q)
                scores.append(s)
        return ((available_queues, np.array(scores)) if available_queues else
                (all_queues, all_scores))

    def get_slots(self) -> Dict:
        return (self.spider.crawler.engine.downloader.slots
                if self.slots_mock is None else self.slots_mock)

    def has_free_slots(self, queue: bytes, slots: Dict) -> bool:
        domain = self.queue_key_domain(queue)
        return domain not in slots or slots[domain].free_transfer_slots()

    @cacheforawhile
    def get_my_queues(self, idx: int, n_idx: int)\
            -> Tuple[List[bytes], np.ndarray]:
        """ Get queues belonging to this worker.
        Here we cache not only expensive redis call, but queue selection too.
        """
        self.try_to_restrict_domains()
        self.set_spider_domain_limit()
        queues = self.get_queues(withscores=True)
        my_queues, my_scores = [], []
        for q, s in queues:
            if crc32(q) % n_idx == idx:
                my_queues.append(q)
                my_scores.append(s)
        return my_queues, np.array(my_scores)

    def discover(self) -> Tuple[int, int]:
        """ Return a tuple of (my index, total number of workers).
        When workers connect or disconnect, this will cause re-distribution
        of domains between workers, but this is not an issue.
        """
        self.im_alive()
        worker_ids = set(map(int, self.get_workers()))
        for worker_id in list(worker_ids):
            if not self.is_alive(worker_id):
                self.server.srem(self.workers_key, worker_id)
                worker_ids.remove(worker_id)
        if self.worker_id in worker_ids:
            worker_ids = sorted(worker_ids)
            return worker_ids.index(self.worker_id), len(worker_ids)
        else:
            # This should not happen normally
            logger.warning('No live workers: selecting self!')
            return 0, 1

    def im_alive(self):
        """ Tell the server that current worker is alive.
        """
        pipe = self.server.pipeline()
        pipe.multi()
        pipe.sadd(self.workers_key, self.worker_id)\
            .set(self._worker_key(self.worker_id), 'ok', ex=self.alive_timeout)\
            .execute()

    def is_alive(self, worker_id) -> bool:
        """ Return whether given worker is alive.
        """
        return bool(self.server.get(self._worker_key(worker_id)))

    def _worker_key(self, worker_id) -> str:
        return self.fkey('worker-{}'.format(worker_id))

    def pop_from_queue(self, queue_key: bytes, n: int) -> List[Request]:
        """ Pop values with highest priorities from the given queue.
        """
        pipe = self.server.pipeline()
        pipe.multi()
        # Get one extra element to know new max score after pop
        pipe.zrange(queue_key, 0, n, withscores=True)\
            .zremrangebyrank(queue_key, 0, n - 1)
        results, count = pipe.execute()
        if results:
            self.server.decr(self.len_key, count)
            if len(results) == n + 1:
                _, queue_score = results[-1]
                self.server.zadd(self.queues_key, queue_score, queue_key)
            else:
                self.remove_queue(queue_key)
            return [self._decode_request_priority(r, -s)
                    for r, s in results[:n]]
        else:
            # queue was empty: remove it from queues set
            self.remove_queue(queue_key)
            return []

    def add_queue(self, queue_key, queue_score: float):
        added = self.server.zadd(self.queues_key, queue_score, queue_key)
        self.update_queue_stats(update_domains=added)
        if added:
            logger.debug('ADD queue {}'.format(queue_key))

    def remove_queue(self, queue_key: bytes) -> None:
        removed = self.server.zrem(self.queues_key, queue_key)
        self.update_queue_stats(update_domains=removed)
        if removed:
            logger.debug('REM queue {}'.format(queue_key))

    def url_queue_key(self, url: str) -> str:
        """ Key for request queue (based on it's SLD).
        """
        return self.fkey('domain:{}'.format(get_domain(url)))

    def queue_key_domain(self, queue_key: bytes) -> str:
        queue_key = queue_key.decode('utf8')
        prefix = self.fkey('domain:')
        assert queue_key.startswith(prefix)
        return queue_key[len(prefix):]

    def get_stats(self):
        """ Return all queue stats.
        """
        queues = self.get_queues(withscores=True)
        return dict(
            len=len(self),
            n_domains=len(queues),
            queues=[(name.decode('utf8'), -score, self.server.zcard(name))
                    for name, score in queues],
        )

    def has_login_form(self, url):
        domain = get_domain(url).encode('utf8')
        return self.server.sismember(self.has_login_form_key, domain)

    def add_login_form(self, url):
        domain = get_domain(url).encode('utf8')
        return self.server.sadd(self.has_login_form_key, domain)

    def add_login_credentials(self, url: str, login: str, password: str):
        domain = get_domain(url)
        credentials = json.dumps(
            {'url': url, 'login': login, 'password': password})
        self.server.hset(
            self.login_credentials_key,
            domain.encode('utf8'), credentials.encode('utf8'))

    def get_login_credentials(self, url: str) -> Optional[Dict]:
        domain = get_domain(url)
        value = self.server.hget(
            self.login_credentials_key, domain.encode('utf8'))
        if value:
            return json.loads(value.decode('utf8'))

    @property
    def restrict_domanis(self):
        return self.max_relevant_domains > 0

    def fkey(self, s):
        return '{}:{}'.format(self.key, s)

    def _decode_request_priority(
            self, encoded_request: bytes, priority: float) -> Request:
        request = self._decode_request(encoded_request)
        request.priority = int(priority)
        return request


# A custom table with symbols commonly occurring in URLs
# Can be improved a bit (~2%) if built on a large and diverse URL sample.
smaz_decode = ["http://", "https://", "http://wwww.", "https://wwww.",
               ".com/", ".com", "?", "%"]
smaz_decode += [x for x in smaz.DECODE if ' ' not in x and x not in smaz_decode]
smaz_tree = smaz.make_tree(smaz_decode)


def url_compress(url: str) -> bytes:
    return smaz.compress(url, compression_tree=smaz_tree).encode('latin1')


def url_decompress(data: bytes) -> str:
    return smaz.decompress(data.decode('latin1'), decompress_table=smaz_decode)


class CompactQueue(BaseRequestQueue):
    """ A more compact request representation:
    preserve only url, depth and parent id.
    Priority is stored and set outside of this method.
    """
    no_parent = b'\x00' * 16

    def _encode_request(self, request: Request) -> bytes:
        depth = max(-2**15, min(2**15 - 1, int(request.meta.get('depth', 0))))
        parent = request.meta.get('parent') or self.no_parent
        assert isinstance(parent, bytes) and len(parent) == 16
        return struct.pack('h', depth) + parent + url_compress(request.url)

    def _decode_request(self, data: bytes) -> Request:
        depth_data, parent, url_data = data[:2], data[2:18], data[18:]
        depth, = struct.unpack('h', depth_data)
        if parent == self.no_parent:
            parent = None
        url = url_decompress(url_data)
        return Request(url, meta={'depth': depth, 'parent': parent})


class SoftmaxQueue(CompactQueue):
    def select_best_queue(self, idx: int, n_idx: int) -> Optional[bytes]:
        """ Select queue taking weights into account.
        """
        available_queues, scores = self.get_available_queues(idx, n_idx)
        if available_queues:
            p = get_softmax_p(scores, self.spider.settings)
            queue = np.random.choice(available_queues, p=p)
            slots = self.get_slots()
            if not self.has_free_slots(queue, slots):
                # It's possible to sample more than one queue above and check
                # which one has free slots. But for now log when we select a
                # "bad" queue - it seems to be extremely rare in practice.
                logger.info('Selected queue has no free slots')
            return queue


def get_softmax_p(scores, settings):
    temprature = (
        settings.getfloat('DD_BALANCING_TEMPERATURE', 0.1) *
        settings.getfloat('DD_PRIORITY_MULTIPLIER', 10000))
    return softmax(-scores, t=temprature)


class BatchQueue(CompactQueue):
    """ Adds batching of requests during pop: a QUEUE_BATCH_SIZE requests are
    popped at once to the local queue, and are then used until the local queue
    is empty.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.local_queue = []

    def __len__(self):
        # FIXME - this is not quite correct, because super().__len__ is total
        # queue size, but self.local_queue is for this worker only.
        return super().__len__() + len(self.local_queue)

    def pop(self, timeout=0) -> Optional[Request]:
        self.update_queue_stats()
        self.local_queue = self.local_queue or self.pop_multi()
        if self.local_queue:
            # TODO - take free slots into account
            return self.local_queue.pop()

    def pop_multi(self) -> List[Request]:
        idx, n_idx = self.discover()
        queues = self.select_best_queues(idx, n_idx)
        queue_counts = Counter(queues)
        requests = []
        unique_queues = set()
        for queue, n in queue_counts.items():
            rs = reversed(self.pop_from_queue(queue, n))
            if rs:
                requests.extend(rs)
                unique_queues.add(queue)
        logger.info('Got {} requests (out of {}) from {} unique queues'.format(
            len(requests), len(queues), len(unique_queues)))
        return requests

    def select_best_queues(self, idx: int, n_idx: int) -> List[bytes]:
        """ Return a list of self.batch_size (if possible)
        queues with repetition.
        """
        available_queues, scores = self.get_my_queues(idx, n_idx)
        if available_queues:
            return list(
                np.random.choice(available_queues, size=self.batch_size))
        else:
            return []

    @property
    def batch_size(self):
        return self.spider.settings.getint('QUEUE_BATCH_SIZE', 100)


class BatchSoftmaxQueue(BatchQueue):
    """ BatchQueue with queues chosen using softmax over queue priorities.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        scores_log = self.spider.settings.get('QUEUE_SCORES_LOG')
        self.scores_log = gzip.open(scores_log, 'at') if scores_log else None

    def select_best_queues(self, idx: int, n_idx: int) -> List[bytes]:
        available_queues, scores = self.get_my_queues(idx, n_idx)
        return self.select_queues_softmax(available_queues, scores) \
            if available_queues else []

    def select_queues_softmax(
            self, available_queues: List[bytes], scores: np.ndarray)\
            -> List[bytes]:
        """ Select self.batch_size queues (with repetition) using softmax.
        Try to select not greater than max_queue_n of each queue.
        """
        p = get_softmax_p(scores, self.spider.settings)
        max_queue_n = int(math.ceil(self.spider.settings.getint(
            'CONCURRENT_REQUESTS_PER_DOMAIN') * 0.5))
        min_n_queues = int(math.ceil(self.batch_size / max_queue_n))
        queues = np.random.choice(
            available_queues, p=p, size=self.batch_size)
        n_unique = len(set(queues))
        if n_unique < min_n_queues:
            logger.info(
                'Resampling without replacement due to low number of '
                'unique queues: got {} unique with {} total, '
                'while wanted at least {} unique'
                .format(n_unique, len(queues), min_n_queues))
            try:
                unique_queues = np.random.choice(
                    available_queues,
                    p=p, replace=False,
                    size=min(len(available_queues), min_n_queues))
            except ValueError:
                # Less non-zero entries in p than size:
                # get all non-zero, and sample at random from the rest
                logger.info(
                    'Resampling due to low number of non-zero entries in p, '
                    'min score is {}'.format(min(scores)))
                unique_queues = list(
                    np.array(available_queues)[np.nonzero(p)][:self.batch_size])
                unique_queues_set = set(unique_queues)
                if len(unique_queues_set) < min_n_queues:
                    unique_queues_set.update(np.random.choice(
                        available_queues,
                        size=min(len(available_queues),
                                 min_n_queues - len(unique_queues))))
                # want to have unique_queues selected with p > 0 at the start
                unique_queues.extend(unique_queues_set - set(unique_queues))
            queues = []
            while len(queues) < self.batch_size:
                for q in unique_queues:
                    queues.extend([q] * max(0, min(
                        max_queue_n, self.batch_size - len(queues))))
            random.shuffle(queues)
        self.log_scores(available_queues, scores, queues)
        return queues

    def log_scores(self, available_queues, scores, queues):
        if self.scores_log:
            q_to_strs = lambda qs: [q.decode('utf8') for q in qs]
            log_item = dict(
                timestamp=time.time(),
                scores=list(scores),
                available_queues=q_to_strs(available_queues),
                queues=q_to_strs(queues),
            )
            self.scores_log.write(json.dumps(log_item))
            self.scores_log.write('\n')
            self.scores_log.flush()
