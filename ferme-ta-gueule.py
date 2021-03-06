#!/usr/bin/env python
# vim: ai ts=4 sts=4 et sw=4

import os
import sys
import time
import datetime
import termcolor

import argparse
import elasticsearch

from pprint import pprint

# https://urllib3.readthedocs.org/en/latest/security.html#insecureplatformwarning
import urllib3
#urllib3.disable_warnings()
import logging
logging.captureWarnings(True)

level = None
INDEX = 'logs'
MAX_PACKETS = 100
url = 'https://elasticsearch.easyflirt.com:443'
LEVELSMAP = {
    'WARN':     logging.WARNING,
    'warning':  logging.WARNING,
    'err':      logging.ERROR,
    'alert':    logging.ERROR,
    'ERROR':    logging.ERROR,
    'FATAL':    logging.CRITICAL,
}
COLORS = {
    'DEBUG': 'white',
    'INFO': 'cyan',
    'WARNING': 'yellow',
    'ERROR': 'white',
    'CRITICAL': 'yellow',
}
ON_COLORS = {
    'CRITICAL': 'on_red',
}
COLORS_ATTRS = {
    'CRITICAL': ('bold',),
    'WARNING': ('bold',),
    'ERROR': ('bold',),
    'DEBUG': ('dark',),
}

class ColoredFormatter(logging.Formatter): # {{{

    def __init__(self):
        # main formatter:
        logformat = '%(message)s'
        logdatefmt = '%H:%M:%S %d/%m/%Y'
        logging.Formatter.__init__(self, logformat, logdatefmt)

    def format(self, record):
        if record.levelname in self.COLORS:
            color = self.COLORS[record.levelname]
            try:
                on_color = self.ON_COLORS[record.levelname]
            except KeyError:
                on_color = None
            try:
                color_attr = self.COLORS_ATTRS[record.levelname]
            except KeyError:
                color_attr = None
            record.msg = u'%s'%termcolor.colored(record.msg, color, on_color, color_attr)
        return logging.Formatter.format(self, record)

# }}}


def getTerminalSize(): # {{{
    rows, columns = os.popen('stty size', 'r').read().split()
    return (rows, columns)
# }}}


def pattern_to_es(pattern):
    if not pattern.startswith('/') and not pattern.startswith('*') and not pattern.endswith('*'):
        pattern = '*' + pattern + '*'
    return pattern.replace(" ", ' AND ')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", help="Do not truncate output", action="store_true")
    parser.add_argument("--error", help="Only errors", action="store_true")
    parser.add_argument("--fatal", help="Only fatals", action="store_true")
    parser.add_argument("--notice", help="Only notices", action="store_true")
    parser.add_argument("--from", help="Starts from N hours ago", action="store", type=int, dest="_from")
    parser.add_argument("--progress", help="Progress bar", action="store_true")
    parser.add_argument("--grep", help="grep pattern. Use /pattern/ for regex search.", action="store")
    parser.add_argument("--exclude", help="grep pattern. Use /pattern/ for regex exclusion.", action="store")
    parser.add_argument("--id", help="get specific id in ES index", action="store")
    parser.add_argument("--interval", help="interval between queries, default 1s", action="store", type=float, default=1)
    parser.add_argument("--url", help="Use another ES", action="store", default=url)
    args = parser.parse_args()

    es = elasticsearch.Elasticsearch(
        args.url, 
        use_ssl=("https" in args.url),
        verify_certs=False
    )

    if args.id:
        tries = 1
        while True:
            try:
                doc = es.get(index=INDEX, id=args.id)
                print "RESULT for ES#%s (%d tries) :" % (args.id, tries)
                for k, v in doc['_source'].items():
                    print "%-14s: %s"%(k, v)
                break
            except elasticsearch.exceptions.NotFoundError:
                if tries >= 4:
                    print "Not Found."
                    sys.exit(42)
                else:
                    tries += 1
        sys.exit(0)

    logging.getLogger('elasticsearch').setLevel(logging.WARNING)
    loghandler = logging.StreamHandler()
    #loghandler.setFormatter(ColoredFormatter())
    logs = logging.getLogger('logs')
    while len(logs.handlers) > 0:
        logs.removeHandler(logs.handlers[0])

    logs.addHandler(loghandler)
    logs.setLevel(logging.DEBUG)

    logs.info("[%s] %d logs in ElasticSearch index", args.url, es.count(INDEX)['count'])

    if args.notice:
        level = " ".join([k for k, v in LEVELSMAP.items() if v == logging.DEBUG])
    elif args.error:
        level = " ".join([k for k, v in LEVELSMAP.items() if v >= logging.ERROR])
    elif args.fatal:
        level = " ".join([k for k, v in LEVELSMAP.items() if v == logging.CRITICAL])


    if args._from:
        now = int(time.time()) - 3600 * args._from
    else:
        now = int(time.time()) - 3600 - 60
    lasts = []
    stats = {'levels': {}}
    laststats = time.time()
    progress = False
    maxp = MAX_PACKETS
    query = {"filter": {"range": {"timestamp": {"gte": now}}}}
    if level:
        try:
            query['query']['bool']['must'].append({'match': {'level': {'query': level, 'operator' : 'or'}}})
        except KeyError:
            query['query'] = {'bool': {'must': [{'match': {'level': {'query': level, 'operator' : 'or'}}}]}}
        now -= 60

    if args.grep:
        grep = pattern_to_es(args.grep)
            
        try:
            query['query']['bool']['must'].append({'query_string': {'fields': ['msg'], 'query': grep}})
        except KeyError:
            query['query'] = {'bool': {'must': [{'query_string': {'fields': ['msg'], 'query': grep}}]}}
        now -= 60

    if args.exclude:
        exclude = pattern_to_es(args.exclude)
            
        try:
            query['query']['bool']['must_not'].append({'query_string': {'fields': ['msg'], 'query': exclude}})
        except KeyError:
            query['query'] = {'bool': {'must_not': [{'query_string': {'fields': ['msg'], 'query': exclude}}]}}
        now -= 60

    logs.debug("ES query: %s"%query)

    try:
        while True:
            #sys.stdout.write('#')
            #sys.stdout.flush()
            query['filter']['range']['timestamp']['gte'] = now
            try:
                s = es.search(INDEX, body=query, sort="timestamp:asc", size=maxp)
            except elasticsearch.exceptions.ConnectionError:
                logs.warning("ES connection error", exc_info=True)
                time.sleep(1)
                continue
            except elasticsearch.exceptions.TransportError:
                logs.critical("Elasticsearch is unreachable, will retry in 1s ...")
                time.sleep(1)
                continue

            if s['hits']['total'] <= len(lasts):
                if progress:
                    if args.progress:
                        sys.stdout.write('.')
                        sys.stdout.flush()
                else:
                    if time.time() - laststats >= 60:
                        laststats = time.time()
                        idx_count = es.count(INDEX)['count']
                        statsmsg = 'STATS: %d logs, '%idx_count
                        for l in stats['levels'].keys():
                            statsmsg += "%s=%d, "%(l, stats['levels'][l])
                        logs.info(statsmsg[:-2])
                progress = True
            else:
                if progress:
                    progress = False
                    if args.progress:
                        sys.stdout.write("\n")
                for ids in s['hits']['hits']:
                    newnow = int(ids['_source']['timestamp'])
                    _id = ids['_id']

                    if not _id in lasts:
                        prettydate = datetime.datetime.fromtimestamp(newnow).strftime('%d-%m-%Y %H:%M:%S')
                        try:
                            loglvl = ids['_source']['level']
                            lvl = LEVELSMAP[loglvl]
                        except KeyError:
                            lvl = logging.DEBUG

                        logmsg = ids['_source']['msg']
                        if not args.full:
                            logmsg = logmsg[:200]

                        color = COLORS[logging.getLevelName(lvl)]
                        try:
                            on_color = ON_COLORS[logging.getLevelName(lvl)]
                        except KeyError:
                            on_color = None
                        try:
                            color_attr = COLORS_ATTRS[logging.getLevelName(lvl)]
                        except KeyError:
                            color_attr = None
                        #record.msg = u'%s'%termcolor.colored(record.msg, color, on_color, color_attr)
                        msg = termcolor.colored(prettydate, 'white', 'on_blue', ('bold',))
                        try:
                            msg += termcolor.colored("<%s>"%ids['_source']['level'], color, on_color, color_attr)
                        except KeyError: pass
                        msg += "(%s) %s >> "%(_id, ids['_source']['program'])
                        msg += termcolor.colored(logmsg, color, on_color, color_attr)

                        logs.log(lvl, msg)
                            


                        try:
                            stats['levels'][ids['_source']['level']] += 1
                        except KeyError:
                            try:
                                stats['levels'][ids['_source']['level']] = 1
                            except KeyError: pass

                    if newnow == now:
                        if not _id in lasts:
                            lasts.append(_id)
                        # Max packets reached
                        if len(s['hits']['hits']) == maxp:
                            maxp += MAX_PACKETS
                    else:
                        maxp = MAX_PACKETS
                        lasts = [_id]

                    now = newnow
            time.sleep(args.interval)
    except KeyboardInterrupt: pass
