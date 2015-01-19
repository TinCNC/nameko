"""
Provides classes and method to deal with dependency injection.
"""
from __future__ import absolute_import

from functools import partial
import inspect
import types
import weakref

from eventlet.event import Event

from logging import getLogger
_log = getLogger(__name__)


ENTRYPOINT_EXTENSIONS_ATTR = 'nameko_entrypoints'

shared_extensions = weakref.WeakKeyDictionary()


class Extension(object):
    """ Note that Extension.__init__ is called during :meth:`bind` as
    well as at instantiation time, so avoid side-effects in this method.
    Use :meth:`setup` instead.
    """
    __clone = False
    __params = None

    def __new__(cls, *args, **kwargs):
        inst = super(Extension, cls).__new__(cls, *args, **kwargs)
        inst.__params = (args, kwargs)
        return inst

    def setup(self, container):
        """ Called before the service container starts.

        Extensions should do any required initialisation here.
        """

    def start(self):
        """ Called when the service container has successfully started.

        This is only called after all other Extensions have successfully
        returned from :meth:`Extension.setup`. If the Extension reacts
        to external events, it should now start acting upon them.
        """

    def stop(self):
        """ Called when the service container begins to shut down.

        Extensions should do any graceful shutdown here.
        """

    def kill(self):
        """ Called to stop this extension without grace.

        Extensions should urgently shut down here. This means
        stopping as soon as possible by omitting cleanup.
        This may be distinct from ``stop()`` for certain dependencies.

        For example, :class:`~messaging.QueueConsumer` tracks messages being
        processed and pending message acks. Its ``kill`` implementation
        discards these and disconnects from rabbit as soon as possible.

        Extensions should not raise during kill, since the container
        is already dying. Instead they should log what is appropriate and
        swallow the exception to allow the container kill to continue.
        """

    def clone(self, container):
        if self.is_clone:
            raise RuntimeError('Cloned extensions cannot be cloned.')

        cls = type(self)
        args, kwargs = self.__params
        instance = cls(*args, **kwargs)
        instance.__clone = True

        # recursive over sub-extensions
        for ext_name, ext in inspect.getmembers(self, is_extension):
            setattr(instance, ext_name, ext.clone(container))

        return instance

    @property
    def is_clone(self):
        return self.__clone is True

    def __repr__(self):
        if not self.is_clone:
            return '<{} [declaration] at 0x{:x}>'.format(
                type(self).__name__, id(self))

        return '<{} at 0x{:x}>'.format(
            type(self).__name__, id(self))


class SharedExtension(Extension):

    @property
    def sharing_key(self):
        return type(self)

    def clone(self, container):
        """ Clone implementation that supports sharing.
        """
        # if there's already a cloned instance, return that
        shared_extensions.setdefault(container, {})
        shared = shared_extensions[container].get(self.sharing_key)
        if shared:
            return shared

        instance = super(SharedExtension, self).clone(container)

        # save the new instance
        shared_extensions[container][self.sharing_key] = instance

        return instance


class Dependency(Extension):

    service_name = None
    attr_name = None

    def bind(self, service_name, attr_name):
        """
        """
        self.service_name = service_name
        self.attr_name = attr_name

    def acquire_injection(self, worker_ctx):
        """ Called before worker execution. A Dependency should return
        an object to be injected into the worker instance by the container.
        """

    def inject(self, worker_ctx):
        """
        """
        injection = self.acquire_injection(worker_ctx)
        setattr(worker_ctx.service, self.attr_name, injection)

    def worker_result(self, worker_ctx, result=None, exc_info=None):
        """ Called with the result of a service worker execution.

        Dependencies that need to process the result should do it here.
        This method is called for all `Dependency` instances on completion
        of any worker.

        Example: a database session dependency may flush the transaction

        :Parameters:
            worker_ctx : WorkerContext
                See ``nameko.containers.ServiceContainer.spawn_worker``
        """

    def worker_setup(self, worker_ctx):
        """ Called before a service worker executes a task.

        Dependencies should do any pre-processing here, raising exceptions
        in the event of failure.

        Example: ...

        :Parameters:
            worker_ctx : WorkerContext
                See ``nameko.containers.ServiceContainer.spawn_worker``
        """

    def worker_teardown(self, worker_ctx):
        """ Called after a service worker has executed a task.

        Dependencies should do any post-processing here, raising
        exceptions in the event of failure.

        Example: a database session dependency may commit the session

        :Parameters:
            worker_ctx : WorkerContext
                See ``nameko.containers.ServiceContainer.spawn_worker``
        """

    def __repr__(self):
        if not self.is_clone:
            return '<{} [declaration] at 0x{:x}>'.format(
                type(self).__name__, id(self))

        if self.service_name is None or self.attr_name is None:
            return '<{} [unbound] at 0x{:x}>'.format(
                type(self).__name__, id(self))

        return '<{} [{}.{}] at 0x{:x}>'.format(
            type(self).__name__, self.service_name, self.attr_name, id(self))


class ProviderCollector(object):
    def __init__(self, *args, **kwargs):
        self._providers = set()
        self._providers_registered = False
        self._last_provider_unregistered = Event()
        super(ProviderCollector, self).__init__(*args, **kwargs)

    def register_provider(self, provider):
        self._providers_registered = True
        _log.debug('registering provider %s for %s', provider, self)
        self._providers.add(provider)

    def unregister_provider(self, provider):
        providers = self._providers
        if provider not in self._providers:
            return

        _log.debug('unregistering provider %s for %s', provider, self)

        providers.remove(provider)
        if len(providers) == 0:
            _log.debug('last provider unregistered for %s', self)
            self._last_provider_unregistered.send()

    def wait_for_providers(self):
        """ Wait for any providers registered with the collector to have
        unregistered.

        Returns immediately if no providers were ever registered.
        """
        if self._providers_registered:
            _log.debug('waiting for providers to unregister %s', self)
            self._last_provider_unregistered.wait()
            _log.debug('all providers unregistered %s', self)

    def stop(self):
        """ Default `:meth:Extension.stop()` implementation for
        subclasses using `ProviderCollector` as a mixin.
        """
        self.wait_for_providers()


def register_entrypoint(fn, entrypoint):
    descriptors = getattr(fn, ENTRYPOINT_EXTENSIONS_ATTR, None)

    if descriptors is None:
        descriptors = set()
        setattr(fn, ENTRYPOINT_EXTENSIONS_ATTR, descriptors)

    descriptors.add(entrypoint)


class Entrypoint(Extension):

    service_name = None
    method_name = None

    def bind(self, service_name, method_name):
        """
        """
        self.service_name = service_name
        self.method_name = method_name

    @classmethod
    def decorator(cls, *args, **kwargs):

        def registering_decorator(fn, args, kwargs):
            instance = cls(*args, **kwargs)
            register_entrypoint(fn, instance)
            return fn

        if len(args) == 1 and isinstance(args[0], types.FunctionType):
            # usage without arguments to the decorator:
            # @foobar
            # def spam():
            #     pass
            return registering_decorator(args[0], args=(), kwargs={})
        else:
            # usage with arguments to the decorator:
            # @foobar('shrub', ...)
            # def spam():
            #     pass
            return partial(registering_decorator, args=args, kwargs=kwargs)

    def __repr__(self):
        if not self.is_clone:
            return '<{} [declaration] at 0x{:x}>'.format(
                type(self).__name__, id(self))

        if self.service_name is None or self.method_name is None:
            return '<{} [unbound] at 0x{:x}>'.format(
                type(self).__name__, id(self))

        return '<{} [{}.{}] at 0x{:x}>'.format(
            type(self).__name__, self.service_name, self.method_name, id(self))


def is_extension(obj):
    return isinstance(obj, Extension)


def is_dependency(obj):
    return isinstance(obj, Dependency)


def is_entrypoint(obj):
    return isinstance(obj, Entrypoint)


def iter_extensions(extension):
    for _, ext in inspect.getmembers(extension, is_extension):
        for item in iter_extensions(ext):
            yield item
        yield ext