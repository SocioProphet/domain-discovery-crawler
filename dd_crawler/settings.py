BOT_NAME = 'dd_crawler'

SPIDER_MODULES = ['dd_crawler.spiders']
NEWSPIDER_MODULE = 'dd_crawler.spiders'

CDR_TEAM = 'HG'
CDR_CRAWLER = 'scrapy dd-crawler'

USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/51.0.2704.84 Safari/537.36')

# Scrapy-redis settings
# Enables scheduling storing requests queue in redis.
SCHEDULER = 'scrapy_redis.scheduler.Scheduler'
DUPEFILTER_CLASS = 'dd_crawler.dupefilter.LoginAwareDupefilter'
# Don't cleanup redis queues, allows to pause/resume crawls.
SCHEDULER_PERSIST = True
# SCHEDULER_QUEUE_CLASS = 'dd_crawler.queue.CompactQueue'
SCHEDULER_QUEUE_CLASS = 'dd_crawler.queue.BatchSoftmaxQueue'
QUEUE_BATCH_SIZE = 100

COMMANDS_MODULE = 'dd_crawler.commands'

DOMAIN_LIMIT = False
RESET_DEPTH = False

DD_PRIORITY_MULTIPLIER = 10000
DD_BALANCING_TEMPERATURE = 0.1
DD_MAX_SCORE = 10 * DD_PRIORITY_MULTIPLIER

# Set to better handle redirects when using non-batch queues
# REDIRECT_PRIORITY_ADJUST = 10 * DD_PRIORITY_MULTIPLIER
REDIRECT_PRIORITY_ADJUST = 1

DEPTH_PRIORITY = 1

AUTOLOGIN_URL = 'http://127.0.0.1:8089'
AUTOLOGIN_ENABLED = False

DOWNLOADER_MIDDLEWARES = {
    'proxy_middleware.ProxyOnlyTorMiddleware': 10,
    'scrapy.downloadermiddlewares.redirect.RedirectMiddleware': None,
    'dd_crawler.middleware.domains.ForbidOffsiteRedirectsMiddleware': 600,
    'dd_crawler.middleware.DDAutologinMiddleware': 605,
    'scrapy.downloadermiddlewares.cookies.CookiesMiddleware': None,
    'autologin_middleware.ExposeCookiesMiddleware': 700,
    'dd_crawler.middleware.domain_status.DomainStatusMiddleware': 1000,
}

MAX_DUPLICATE_PATH_SEGMENTS = 5
MAX_DUPLICATE_QUERY_SEGMENTS = 3

SPIDER_MIDDLEWARES = {
    'dd_crawler.middleware.domains.DomainControlMiddleware': 550,
    'dd_crawler.middleware.log.RequestLogMiddleware': 600,
    'dd_crawler.middleware.dupesegments.DupeSegmentsMiddleware': 750,
}

ITEM_PIPELINES = {
    'scrapy_cdr.media_pipeline.CDRMediaPipeline': 1,
}

MEDIA_ALLOW_REDIRECTS = True

FEED_STORAGES = {
    'gzip': 'deepdeep.exports.GzipFileFeedStorage',
}

EXTENSIONS = {
    'deepdeep.extensions.DumpStatsExtension': 101,
}

HTTPCACHE_ENABLED = False
REDIRECT_ENABLED = True
COOKIES_ENABLED = True
DOWNLOAD_TIMEOUT = 240
RETRY_ENABLED = False
DOWNLOAD_MAXSIZE = 1*1024*1024

# Auto throttling
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_DEBUG = False
AUTOTHROTTLE_MAX_DELAY = 3.0
AUTOTHROTTLE_START_DELAY = 1.0
RANDOMIZE_DOWNLOAD_DELAY = False

# Concurrency
CONCURRENT_REQUESTS = 64
CONCURRENT_REQUESTS_PER_DOMAIN = 10
DOWNLOAD_DELAY = 0.0

REACTOR_THREADPOOL_MAXSIZE = 32
DNS_TIMEOUT = 180

LOG_LEVEL = 'INFO'

# Uncommend to enable collection of statsd
# STATS_CLASS = 'scrapy_statsd.statscollectors.StatsDStatsCollector'
# STATSD_HOST = 'localhost'
# STATSD_PORT = 80125
import socket
STATSD_PREFIX = socket.gethostname().replace('.', '-')
