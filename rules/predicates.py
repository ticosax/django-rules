from functools import partial, update_wrapper
import inspect
import threading


class Context(dict):
    def __init__(self, args):
        super(Context, self).__init__()
        self.args = args


class localcontext(threading.local):
    def __init__(self):
        self.stack = []


_context = localcontext()


NO_VALUE = frozenset()


class Predicate(object):
    def __init__(self, fn, name=None):
        # fn can be a callable with any of the following signatures:
        #   - fn(obj=None, target=None)
        #   - fn(obj=None)
        #   - fn()
        assert callable(fn), 'The given predicate is not callable.'
        if isinstance(fn, Predicate):
            fn, num_args, var_args, name = fn.fn, fn.num_args, fn.var_args, name or fn.name
        elif isinstance(fn, partial):
            argspec = inspect.getargspec(fn.func)
            num_args = len(argspec.args) - len(fn.args)
            var_args = argspec.varargs is not None
            name = fn.func.__name__
        elif inspect.isfunction(fn):
            argspec = inspect.getargspec(fn)
            var_args = argspec.varargs is not None
            num_args = len(argspec.args)
        elif isinstance(fn, object):
            callfn = getattr(fn, '__call__')
            argspec = inspect.getargspec(callfn)
            var_args = argspec.varargs is not None
            num_args = len(argspec.args) - 1  # skip `self`
            name = name or type(fn).__name__
        else:
            raise TypeError('Incompatible predicate.')
        assert num_args <= 2, 'Incompatible predicate.'
        self.fn = fn
        self.num_args = num_args
        self.var_args = var_args
        self.name = name or fn.__name__

    def __repr__(self):
        return '<%s:%s object at %s>' % (
            type(self).__name__, str(self), hex(id(self)))

    def __str__(self):
        return self.name

    def __call__(self, *args, **kwargs):
        # this method is defined as variadic in order to not mask the
        # underlying callable's signature that was most likely decorated
        # as a predicate. internally we consistently call ``_apply`` that
        # provides a single interface to the callable.
        return self.fn(*args, **kwargs)

    @property
    def context(self):
        """
        The currently active invocation context. A new context is created as a
        result of invoking ``test()`` and is only valid for the duration of
        the invocation.

        Can be used by predicates to store arbitrary data, eg. for caching
        computed values, setting flags, etc., that can be used by predicates
        later on in the chain.

        Inside a predicate function it can be used like so::

            >>> @predicate
            ... def mypred(a, b):
            ...     value = compute_expensive_value(a)
            ...     mypred.context['value'] = value
            ...     return True
            ...

        Other predicates can later use stored values::

            >>> @predicate
            ... def myotherpred(a, b):
            ...     value = myotherpred.context.get('value')
            ...     if value is not None:
            ...         return do_something_with_value(value)
            ...     else:
            ...         return do_something_without_value()
            ...

        """
        try:
            return _context.stack[-1]
        except IndexError:
            return None

    def test(self, obj=NO_VALUE, target=NO_VALUE):
        """
        The canonical method to invoke predicates.
        """
        args = tuple(arg for arg in (obj, target) if arg is not NO_VALUE)
        _context.stack.append(Context(args))
        try:
            return self._apply(*args)
        finally:
            _context.stack.pop()

    def __and__(self, other):
        def AND(*args):
            return self._apply(*args) and other._apply(*args)
        return type(self)(AND, '(%s & %s)' % (self.name, other.name))

    def __or__(self, other):
        def OR(*args):
            return self._apply(*args) or other._apply(*args)
        return type(self)(OR, '(%s | %s)' % (self.name, other.name))

    def __xor__(self, other):
        def XOR(*args):
            return self._apply(*args) ^ other._apply(*args)
        return type(self)(XOR, '(%s ^ %s)' % (self.name, other.name))

    def __invert__(self):
        def INVERT(*args):
            return not self._apply(*args)
        if self.name.startswith('~'):
            name = self.name[1:]
        else:
            name = '~' + self.name
        return type(self)(INVERT, name)

    def _apply(self, *args):
        # Internal method that is used to invoke the predicate with the
        # proper number of positional arguments, inside the current
        # invocation context.
        if self.var_args:
            callargs = args
        elif self.num_args > len(args):
            callargs = args + (None,) * (self.num_args - len(args))
        else:
            callargs = args[:self.num_args]
        return bool(self.fn(*callargs))


def predicate(fn=None, name=None):
    """
    Decorator that constructs a ``Predicate`` instance from any function::

        >>> @predicate
        ... def is_book_author(user, book):
        ...     return user == book.author
        ...
    """
    if not name and not callable(fn):
        name = fn
        fn = None

    def inner(fn):
        if isinstance(fn, Predicate):
            return fn
        p = Predicate(fn, name)
        if isinstance(fn, partial):
            update_wrapper(p, fn.func)
        else:
            update_wrapper(p, fn)
        return p

    if fn:
        return inner(fn)
    else:
        return inner


# Predefined predicates

always_true = predicate(lambda: True, name='always_true')
always_false = predicate(lambda: False, name='always_false')

always_allow = predicate(lambda: True, name='always_allow')
always_deny = predicate(lambda: False, name='always_deny')


@predicate
def is_authenticated(user):
    if not hasattr(user, 'is_authenticated'):
        return False  # not a user model
    return user.is_authenticated()


@predicate
def is_superuser(user):
    if not hasattr(user, 'is_superuser'):
        return False  # swapped user model, doesn't support is_superuser
    return user.is_superuser


@predicate
def is_staff(user):
    if not hasattr(user, 'is_staff'):
        return False  # swapped user model, doesn't support is_staff
    return user.is_staff


@predicate
def is_active(user):
    if not hasattr(user, 'is_active'):
        return False  # swapped user model, doesn't support is_active
    return user.is_active


def is_group_member(*groups):
    assert len(groups) > 0, 'You must provide at least one group name'

    if len(groups) > 3:
        g = groups[:3] + ('...',)
    else:
        g = groups

    name = 'is_group_member:%s' % ','.join(g)

    @predicate(name)
    def fn(user):
        if not hasattr(user, 'groups'):
            return False  # swapped user model, doesn't support groups
        if not hasattr(user, '_group_names_cache'):
            user._group_names_cache = set(user.groups.values_list('name', flat=True))
        return set(groups).issubset(user._group_names_cache)

    return fn
