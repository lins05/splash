import os
import json
import uuid
import time

import redis

from twisted.internet import defer
from twisted.python import log


class RenderPool(object):
    """A pool of renders. The number of slots determines how many
    renders will be run in parallel, at the most."""

    def __init__(self, slots, network_manager_factory, splash_proxy_factory_cls, js_profiles_path, verbosity=1):
        self.network_manager_factory = network_manager_factory
        self.splash_proxy_factory_cls = splash_proxy_factory_cls or (lambda profile_name: None)
        self.js_profiles_path = js_profiles_path
        self.active = set()
        self.queue = defer.DeferredQueue()
        self.verbosity = verbosity
        for n in range(slots):
            self._wait_for_render(None, n, log=False)

        self.debug_key = 'splash-urls-{}'.format(os.environ.get('MESOS_TASK_ID', str(uuid.uuid4())))
        redis_host = os.environ.get('REDIS_HOST', '127.0.0.1')
        redis_port = int(os.environ.get('REDIS_PORT') or 6379)
        log.msg('redis host %s, redis port %s' % (redis_host, redis_port), system='debug-urls')
        self.debug_dbc = redis.StrictRedis(
            host=redis_host,
            port=redis_port,
            socket_connect_timeout=10,
            socket_timeout=10,
            db=0)

    def render(self, rendercls, render_options, proxy, **kwargs):
        splash_proxy_factory = self.splash_proxy_factory_cls(proxy)
        pool_d = defer.Deferred()
        self.queue.put((rendercls, render_options, splash_proxy_factory, kwargs, pool_d))
        self.log("[%s] queued" % render_options.get_uid())
        return pool_d

    def _wait_for_render(self, _, slot, log=True):
        if log:
            self.log("SLOT %d is available" % slot)
        d = self.queue.get()
        d.addCallback(self._start_render, slot)
        d.addBoth(self._wait_for_render, slot)
        return _

    def _start_render(self, slot_args, slot):
        self.log("initializing SLOT %d" % (slot, ))
        (rendercls, render_options, splash_proxy_factory, kwargs,
         pool_d) = slot_args
        render = rendercls(
            network_manager=self.network_manager_factory(),
            splash_proxy_factory=splash_proxy_factory,
            render_options=render_options,
            verbosity=self.verbosity,
        )
        self.active.add(render)
        self.debug_update()
        render.deferred.chainDeferred(pool_d)
        pool_d.addErrback(self._error, render, slot)
        pool_d.addBoth(self._close_render, render, slot)

        self.log("[%s] SLOT %d is starting" % (render_options.get_uid(), slot))
        try:
            render.start(**kwargs)
        except:
            render.deferred.errback()
            raise
        self.log("[%s] SLOT %d is working" % (render_options.get_uid(), slot))

        return render.deferred

    def _error(self, failure, render, slot):
        uid = render.render_options.get_uid()
        self.log("[%s] SLOT %d finished with an error %s: %s" % (uid, slot, render, failure))
        return failure

    def _close_render(self, _, render, slot):
        uid = render.render_options.get_uid()
        self.log("[%s] SLOT %d is closing %s" % (uid, slot, render))
        self.active.remove(render)
        self.debug_update()
        render.deferred.cancel()
        render.close()
        self.log("[%s] SLOT %d done with %s" % (uid, slot, render))
        return _

    def log(self, text):
        if self.verbosity >= 2:
            log.msg(text, system='pool')

    def debug_update(self):
        """
        Save active requests to redis for debugging which could crash the
        splash instance.
        """
        urls = [render.render_options.get_url() for render in self.active]
        debug_data = {
            # Save the ts so we can tell crashed splash instances
            'ts': int(time.time()),
            'urls': urls,
        }
        try:
            self.debug_dbc.set(self.debug_key, json.dumps(debug_data).encode())
        except Exception as e:
            log.msg('debug_update failed: %s' % str(e), system='debug-urls')
