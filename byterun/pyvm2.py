"""A pure-Python Python bytecode interpreter."""
# Based on:
# pyvm2 by Paul Swartz (z3p), from http://www.twistedmatrix.com/users/z3p/

import collections, operator, dis, new, inspect, sys, types
CO_GENERATOR = 32 # flag for "this code uses yield"

class Function(object):
    def __init__(self, code, globs, defaults, closure, vm):
        self.vm = vm
        self.func_code = code
        self.func_name = code.co_name
        self.func_defaults = defaults
        self.func_globals = globs
        self.func_dict = vm.frame.f_locals
        self.func_closure = closure

        # Sometimes, we need a real Python function.  This is for that.
        self.func = types.FunctionType(code, globs, argdefs=tuple(defaults))

    def __str__(self):
        return '<function %s at 0x%08X>' % (self.func_name, id(self))

    def __call__(self, *args, **kw):
        if len(args) < self.func_code.co_argcount:
            if not self.func_defaults:
                if self.func_code.co_argcount == 0:
                    argCount = 'no arguments'
                elif self.func_code.co_argcount == 1:
                    argCount = 'exactly 1 argument'
                else:
                    argCount = 'exactly %i arguments' % self.func_code.co_argcount
                raise TypeError, '%s() takes %s (%s given)' % (self.func_name,
                                                               argCount, len(args))
            else:
                defArgCount = len(self.func_defaults)
                args.extend(self.func_defaults[-(self.func_code.co_argcount - len(args)):])
        frame = self.vm.make_frame(self.func_code, args, kw, self.func_globals, self.func_dict)
        return self.vm.run_frame(frame)


class Class(object):
    def __init__(self, name, bases, methods):
        self.name = name
        self.bases = bases
        self.locals = methods

    def __call__(self, *args, **kw):
        return Object(self, self.locals, args, kw)

    def __str__(self):
        return '<class %s at 0x%08X>' % (self.name, id(self))


class Object(object):
    def __init__(self, _class, methods, args, kw):
        self._class = _class
        self.locals = methods
        if methods.has_key('__init__'):
            methods['__init__'](self, *args, **kw)

    def __str__(self):
        return '<%s instance at 0x%08X>' % (self._class.name, id(self))

    def __getattr__(self, name):
        try:
            val = self.locals[name]
        except KeyError:
            raise AttributeError
        if isinstance(val, Function):
            val = Method(self, self._class, val)
        return val


class Method:
    def __init__(self, obj, _class, func):
        self.im_self = obj
        self.im_class = _class
        self.im_func = func

    def __str__(self):
        if self.im_self:
            return '<bound method %s.%s of %s>' % (self.im_self.name,
                                                   self.im_func.func_name,
                                                   str(self.im_self))
        else:
            return '<unbound method %s.%s>' % (self.im_class.name,
                                               self.im_func.func_name)


class Cell(object):
    """A fake cell for closures.

    Closures keep names in scope by storing them not in a frame, but in a
    separate object called a cell.  Frames share references to cells, and
    the LOAD_DEREF and STORE_DEREF opcodes get and set the value from cells.

    This class acts as a cell, though it has to jump through two hoops to make
    the simulation complete:

        1. In order to create actual FunctionType functions, we have to have
           actual cell objects, which are difficult to make. See the twisty
           double-lambda in __init__.

        2. Actual cell objects can't be modified, so to implement STORE_DEREF,
           we store a one-element list in our cell, and then use [0] as the
           actual value.

    """
    def __init__(self, value):
        # Thanks to Alex Gaynor for help with this bit of twistiness.
        # Construct an actual cell object by creating a closure right here,
        # and grabbing the cell object out of the function we create.
        # Note the [value] that makes a one-element list so we have
        # writability later.
        self.cell = (lambda x: lambda: x)([value]).func_closure[0]

    def get(self):
        return self.cell.cell_contents[0]

    def set(self, value):
        self.cell.cell_contents[0] = value


Block = collections.namedtuple("Block", "type, handler, level")

class Frame(object):
    def __init__(self, f_code, f_globals, f_locals, f_back):
        self.f_code = f_code
        self.f_globals = f_globals
        self.f_locals = f_locals
        self.f_back = f_back
        if f_back:
            self.f_builtins = f_back.f_builtins
        else:
            self.f_builtins = f_locals['__builtins__']
            if hasattr(self.f_builtins, '__dict__'):
                self.f_builtins = self.f_builtins.__dict__

        self.f_lineno = f_code.co_firstlineno
        self.f_lasti = 0

        if f_code.co_cellvars:
            self.cells = {}
            if not f_back.cells:
                f_back.cells = {}
            for var in f_code.co_cellvars:
                # Make a cell for the variable in our locals, or None.
                cell = Cell(self.f_locals.get(var))
                f_back.cells[var] = self.cells[var] = cell
        else:
            self.cells = None

        if f_code.co_freevars:
            if not self.cells:
                self.cells = {}
            for var in f_code.co_freevars:
                self.cells[var] = f_back.cells[var]

        self.block_stack = []
        self.generator = None

    def __str__(self):
        return '<frame object at 0x%08X>' % id(self)


class Generator(object):
    def __init__(self, g_frame, vm):
        self.gi_frame = g_frame
        self.vm = vm

    def __iter__(self):
        self.first = True
        self.finished = False
        return self

    def next(self):
        # Ordinary iteration is like sending None into a generator.
        if not self.first:
            self.vm.push(None)
        self.first = False
        # To get the next value from an iterator, push its frame onto the
        # stack, and let it run.
        val = self.vm.resume_frame(self.gi_frame)
        if self.finished:
            raise StopIteration
        return val



class VirtualMachineError(Exception):
    """For raising errors in the operation of the VM."""
    pass

class VirtualMachine(object):
    def __init__(self):
        # The call stack of frames.
        self.frames = []
        # The current frame.
        self.frame = None
        # The data stack.
        self.stack = []
        self.return_value = None
        self.last_exception = None
        self._log = []

    def peek(self):
        return self.stack[-1]

    def pop(self):
        return self.stack.pop()

    def push(self, thing):
        self.stack.append(thing)

    def popn(self, n):
        if n:
            ret = self.stack[-n:]
            self.stack[-n:] = []
            return ret
        else:
            return []

    def jump(self, jump):
        """Move the bytecode pointer to `jump`, so it will execute next."""
        self.frame.f_lasti = jump

    def push_block(self, type, handler):
        self.frame.block_stack.append(Block(type, handler, len(self.stack)))

    def log(self, msg):
        self._log.append(msg)

    def make_frame(self, code, args=[], kw={}, f_globals=None, f_locals=None):
        self.log("make_frame: code=%r, args=%r, kw=%r" % (code, args, kw))
        if f_globals:
            f_globals = f_globals
            if not f_locals:
                f_locals = f_globals
        elif self.frames:
            f_globals = self.frame.f_globals
            f_locals = {}
        else:
            f_globals = f_locals = globals()
        for i in range(code.co_argcount):
            name = code.co_varnames[i]
            if i < len(args):
                if kw.has_key(name):
                    raise TypeError("got multiple values for keyword argument '%s'" % name)
                else:
                    f_locals[name] = args[i]
            else:
                if kw.has_key(name):
                    f_locals[name] = kw[name]
                else:
                    raise TypeError("did not get value for argument '%s'" % name)
        frame = Frame(code, f_globals, f_locals, self.frame)
        return frame

    def push_frame(self, frame):
        self.frames.append(frame)
        self.frame = frame

    def pop_frame(self):
        self.frames.pop()
        if self.frames:
            self.frame = self.frames[-1]
        else:
            self.frame = None

    def resume_frame(self, frame):
        frame.f_back = self.frame
        val = self.run_frame(frame)
        frame.f_back = None
        return val

    def run_code(self, code):
        frame = self.make_frame(code)
        try:
            val = self.run_frame(frame)
        finally:
            # Check some invariants
            if self.frames:            # pragma: no cover
                raise VirtualMachineError("Frames left over!")
            if self.stack:             # pragma: no cover
                raise VirtualMachineError("Data left on stack! %r" % self.stack)

        return val

    def run_frame(self, frame):
        """Run a frame until it returns (somehow).

        Exceptions are raised, the return value is returned.

        """
        self.push_frame(frame)
        while True:
            opoffset = frame.f_lasti
            byte = frame.f_code.co_code[opoffset]
            frame.f_lasti += 1
            byteCode = ord(byte)
            byteName = dis.opname[byteCode]
            arg = None
            arguments = []
            if byteCode >= dis.HAVE_ARGUMENT:
                arg = frame.f_code.co_code[frame.f_lasti:frame.f_lasti+2]
                frame.f_lasti += 2
                intArg = ord(arg[0]) + (ord(arg[1])<<8)
                if byteCode in dis.hasconst:
                    arg = frame.f_code.co_consts[intArg]
                elif byteCode in dis.hasfree:
                    if intArg < len(frame.f_code.co_cellvars):
                        arg = frame.f_code.co_cellvars[intArg]
                    else:
                        arg = frame.f_code.co_freevars[intArg-len(frame.f_code.co_cellvars)]
                elif byteCode in dis.hasname:
                    arg = frame.f_code.co_names[intArg]
                elif byteCode in dis.hasjrel:
                    arg = frame.f_lasti + intArg
                elif byteCode in dis.hasjabs:
                    arg = intArg
                elif byteCode in dis.haslocal:
                    arg = frame.f_code.co_varnames[intArg]
                else:
                    arg = intArg
                arguments = [arg]

            if 1:
                op = "%d: %s" % (opoffset, byteName)
                if arguments:
                    op += " %r" % (arguments[0],)
                self.log("%s%40s %r" % ("  "*(len(self.frames)-1), op, self.stack))

            # When unwinding the block stack, we need to keep track of why we
            # are doing it.
            why = None

            try:
                if byteName.startswith('UNARY_'):
                    self.unaryOperator(byteName[6:])
                elif byteName.startswith('BINARY_'):
                    self.binaryOperator(byteName[7:])
                elif byteName.startswith('INPLACE_'):
                    self.inplaceOperator(byteName[8:])
                elif 'SLICE' in byteName:
                    self.sliceOperator(byteName)
                else:
                    # dispatch
                    func = getattr(self, 'byte_%s' % byteName, None)
                    if not func:            # pragma: no cover
                        raise VirtualMachineError("unknown bytecode type: %s" % byteName)
                    why = func(*arguments)

            except:
                # deal with exceptions encountered while executing the op.
                self.last_exception = sys.exc_info()[:2] + (None,)
                why = 'exception'

            # Deal with any block management we need to do.

            if why == 'exception':
                # TODO: ceval calls PyTraceBack_Here, not sure what that does.
                pass

            if why == 'reraise':
                why = 'exception'

            while why and frame.block_stack:

                block = frame.block_stack[-1]
                if block.type == 'loop' and why == 'continue':
                    self.jump(self.return_value)
                    why = None
                    break

                frame.block_stack.pop()

                while len(self.stack) > block.level:
                    self.pop()

                if block.type == 'loop' and why == 'break':
                    why = None
                    self.jump(block.handler)
                    break

                if (block.type == 'finally' or 
                    (block.type == 'except' and why == 'exception') or
                    block.type == 'with'):

                    if why == 'exception':
                        exctype, value, tb = self.last_exception
                        self.push(tb)
                        self.push(value)
                        self.push(exctype)
                    else:
                        if why in ('return', 'continue'):
                            self.push(self.return_value)
                        self.push(why)

                    why = None
                    self.jump(block.handler)
                    break

            if why:
                break

        self.pop_frame()

        if why == 'exception':
            raise self.last_exception[0], self.last_exception[1], self.last_exception[2]

        return self.return_value

    ## Stack manipulation

    def byte_LOAD_CONST(self, const):
        self.push(const)

    def byte_POP_TOP(self):
        self.pop()

    def byte_DUP_TOP(self):
        self.push(self.peek())

    def byte_DUP_TOPX(self, count):
        items = self.popn(count)
        for i in [1, 2]:
            for x in items:
                self.push(x)

    def byte_ROT_TWO(self):
        a = self.pop()
        b = self.pop()
        self.push(a)
        self.push(b)

    def byte_ROT_THREE(self):
        a = self.pop()
        b = self.pop()
        c = self.pop()
        self.push(a)
        self.push(c)
        self.push(b)

    def byte_ROT_FOUR(self):
        a = self.pop()
        b = self.pop()
        c = self.pop()
        d = self.pop()
        self.push(a)
        self.push(d)
        self.push(c)
        self.push(b)

    ## Names

    def byte_LOAD_NAME(self, name):
        frame = self.frame
        if frame.f_locals.has_key(name):
            item = frame.f_locals[name]
        elif frame.f_globals.has_key(name):
            item = frame.f_globals[name]
        elif frame.f_builtins.has_key(name):
            item = frame.f_builtins[name]
        else:
            raise NameError("name '%s' is not defined" % name)
        self.push(item)

    def byte_STORE_NAME(self, name):
        self.frame.f_locals[name] = self.pop()

    def byte_DELETE_NAME(self, name):
        del self.frame.f_locals[name]

    def byte_LOAD_FAST(self, name):
        self.push(self.frame.f_locals[name])

    def byte_STORE_FAST(self, name):
        self.frame.f_locals[name] = self.pop()

    def byte_LOAD_GLOBAL(self, name):
        f = self.frame
        if f.f_globals.has_key(name):
            self.push(f.f_globals[name])
        elif f.f_builtins.has_key(name):
            self.push(f.f_builtins[name])
        else:
            raise NameError("global name '%s' is not defined" % name)

    def byte_LOAD_DEREF(self, name):
        self.push(self.frame.cells[name].get())

    def byte_STORE_DEREF(self, name):
        self.frame.cells[name].set(self.pop())

    def byte_LOAD_LOCALS(self):
        self.push(self.frame.f_locals)

    ## Operators

    UNARY_OPERATORS = {
        'POSITIVE': operator.pos,
        'NEGATIVE': operator.neg,
        'NOT':      operator.not_,
        'CONVERT':  repr,
        'INVERT':   operator.invert,
    }

    def unaryOperator(self, op):
        one = self.pop()
        self.push(self.UNARY_OPERATORS[op](one))

    BINARY_OPERATORS = {
        'POWER':    pow,
        'MULTIPLY': operator.mul,
        'DIVIDE':   operator.div,
        'MODULO':   operator.mod,
        'ADD':      operator.add,
        'SUBTRACT': operator.sub,
        'SUBSCR':   operator.getitem,
        'LSHIFT':   operator.lshift,
        'RSHIFT':   operator.rshift,
        'AND':      operator.and_,
        'XOR':      operator.xor,
        'OR':       operator.or_,
    }

    def binaryOperator(self, op):
        one = self.pop()
        two = self.pop()
        self.push(self.BINARY_OPERATORS[op](two, one))

    INPLACE_OPERATORS = { # these are execed
        'POWER':    'x **= y',
        'MULTIPLY': 'x *= y',
        'DIVIDE':   'x /= y',
        'MODULO':   'x %= y',
        'ADD':      'x += y',
        'SUBTRACT': 'x -= y',
        'LSHIFT':   'x >>= y',
        'RSHIFT':   'x <<= y',
        'AND':      'x &= y',
        'XOR':      'x ^= y',
        'OR':       'x |= y',
    }
        
    def inplaceOperator(self, op):
        y = self.pop()
        x = self.pop()
        # Isn't there a better way than exec?? :(
        vars = {'x':x, 'y':y}
        exec self.INPLACE_OPERATORS[op] in vars
        self.push(vars['x'])

    def sliceOperator(self, op):
        start = 0
        end = None # we will take this to mean end
        op, count = op[:-2], int(op[-1])
        if count == 1:
            start = self.pop()
        elif count == 2:
            end = self.pop()
        elif count == 3:
            end = self.pop()
            start = self.pop()
        l = self.pop()
        if end == None:
            end = len(l)
        if op.startswith('STORE_'):
            l[start:end] = self.pop()
        elif op.startswith('DELETE_'):
            del l[start:end]
        else:
            self.push(l[start:end])

    COMPARE_OPERATORS = [
        operator.lt,
        operator.le,
        operator.eq,
        operator.ne,
        operator.gt,
        operator.ge,
        operator.contains,
        lambda x,y: x not in y,
        lambda x,y: x is y,
        lambda x,y: x is not y,
        lambda x,y: issubclass(x, Exception) and issubclass(x, y)
    ]
        
    def byte_COMPARE_OP(self, opnum):
        one = self.pop()
        two = self.pop()
        self.push(self.COMPARE_OPERATORS[opnum](two, one))

    ## Attributes and indexing

    def byte_LOAD_ATTR(self, attr):
        obj = self.pop()
        val = getattr(obj, attr)
        self.push(val)

    def byte_STORE_ATTR(self, name):
        obj = self.pop()
        setattr(obj, name, self.pop())

    def byte_DELETE_ATTR(self, name):
        obj = self.pop()
        delattr(obj, name)

    def byte_STORE_SUBSCR(self):
        ind = self.pop()
        l = self.pop()
        item = self.pop()
        l[ind] = item

    def byte_DELETE_SUBSCR(self):
        ind = self.pop()
        l = self.pop()
        del l[ind]

    ## Building

    def byte_BUILD_TUPLE(self, count):
        elts = self.popn(count)
        self.push(tuple(elts))

    def byte_BUILD_LIST(self, count):
        elts = self.popn(count)
        self.push(elts)

    def byte_BUILD_MAP(self, size):
        # size is ignored.
        self.push({})

    def byte_STORE_MAP(self):
        key = self.pop()
        value = self.pop()
        the_map = self.pop()
        the_map[key] = value
        self.push(the_map)

    def byte_UNPACK_SEQUENCE(self, count):
        l = self.pop()
        for x in reversed(l):
            self.push(x)

    ## Printing

    if 0:   # Only used in the interactive interpreter, not in modules.
        def byte_PRINT_EXPR(self):
            print self.pop()

    def byte_PRINT_ITEM(self):
        print self.pop(),

    def byte_PRINT_ITEM_TO(self):
        item = self.pop()
        to = self.peek()
        print >>to, item,

    def byte_PRINT_NEWLINE(self):
        print

    def byte_PRINT_NEWLINE_TO(self):
        to = self.peek()
        print >>to , ''

    ## Jumps

    def byte_JUMP_FORWARD(self, jump):
        self.jump(jump)

    def byte_JUMP_ABSOLUTE(self, jump):
        self.jump(jump)

    if 0:   # Not in py2.7
        def byte_JUMP_IF_TRUE(self, jump):
            val = self.peek()
            if val:
                self.jump(jump)

        def byte_JUMP_IF_FALSE(self, jump):
            val = self.peek()
            if not val:
                self.jump(jump)

    def byte_POP_JUMP_IF_TRUE(self, jump):
        val = self.pop()
        if val:
            self.jump(jump)

    def byte_POP_JUMP_IF_FALSE(self, jump):
        val = self.pop()
        if not val:
            self.jump(jump)

    def byte_JUMP_IF_TRUE_OR_POP(self, jump):
        val = self.peek()
        if val:
            self.jump(jump)
        else:
            self.pop()

    def byte_JUMP_IF_FALSE_OR_POP(self, jump):
        val = self.peek()
        if not val:
            self.jump(jump)
        else:
            self.pop()

    ## Blocks

    def byte_SETUP_LOOP(self, dest):
        self.push_block('loop', dest)

    def byte_GET_ITER(self):
        self.push(iter(self.pop()))

    def byte_FOR_ITER(self, jump):
        iterobj = self.peek()
        try:
            v = iterobj.next()
            self.push(v)
        except StopIteration:
            self.pop()
            self.jump(jump)

    def byte_BREAK_LOOP(self):
        return 'break'

    def byte_CONTINUE_LOOP(self, dest):
        # This is a trick with the return value.
        # While unrolling blocks, continue and return both have to preserve
        # state as the finally blocks are executed.  For continue, it's
        # where to jump to, for return, it's the value to return.  It gets
        # pushed on the stack for both, so continue puts the jump destination
        # into return_value.
        self.return_value = dest
        return 'continue'

    def byte_SETUP_EXCEPT(self, dest):
        self.push_block('except', dest)

    def byte_SETUP_FINALLY(self, dest):
        self.push_block('finally', dest)

    def byte_END_FINALLY(self):
        v = self.pop()
        if isinstance(v, str):
            why = v
            if why in ('return', 'continue'):
                self.return_value = self.pop()
        elif v is None:
            why = None
        elif issubclass(v, BaseException):
            exctype = v
            value = self.pop()
            tb = self.pop()
            self.last_exception = (exctype, value, tb)
            why = 'reraise'
        return why

    def byte_POP_BLOCK(self):
        self.frame.block_stack.pop()

    def byte_RAISE_VARARGS(self, argc):
        exctype = value = tb = None
        if argc == 0:
            exctype, value, tb = self.last_exception
        elif argc == 1:
            exctype = self.pop()
        elif argc == 2:
            exctype, value = self.pop(), self.pop()
        elif argc == 3:
            exctype, value, tb = self.pop(), self.pop(), self.pop()

        # There are a number of forms of "raise", normalize them somewhat.
        if isinstance(exctype, BaseException):
            value = exctype
            exctype = type(value)

        self.last_exception = (exctype, value, tb)

        if tb:
            return 'reraise'
        else:
            return 'exception'

    ## Functions

    def byte_MAKE_FUNCTION(self, argc):
        code = self.pop()
        defaults = self.popn(argc)
        globs = self.frame.f_globals
        fn = Function(code, globs, defaults, None, self)
        self.push(fn)

    def byte_LOAD_CLOSURE(self, name):
        self.push(self.frame.cells[name].cell)

    def byte_MAKE_CLOSURE(self, argc):
        code = self.pop()
        closure = self.pop()
        defaults = self.popn(argc)
        globs = self.frame.f_globals
        fn = types.FunctionType(code, globs, argdefs=tuple(defaults), closure=closure)
        self.push(fn)

    def byte_CALL_FUNCTION(self, arg):
        return self.call_function(arg, [], {})

    def byte_CALL_FUNCTION_VAR(self, arg):
        args = self.pop()
        return self.call_function(arg, args, {})

    def byte_CALL_FUNCTION_KW(self, arg):
        kwargs = self.pop()
        return self.call_function(arg, [], kwargs)

    def byte_CALL_FUNCTION_VAR_KW(self, arg):
        kwargs = self.pop()
        args = self.pop()
        return self.call_function(arg, args, kwargs)

    def call_function(self, arg, args, kwargs):
        lenKw, lenPos = divmod(arg, 256)
        namedargs = {}
        for i in xrange(lenKw):
            value = self.pop()
            key = self.pop()
            namedargs[key] = value
        namedargs.update(kwargs)
        posargs = self.popn(lenPos)
        posargs.extend(args)

        func = self.pop()
        frame = self.frame
        if hasattr(func, 'im_func'):
            # Methods get self as an implicit first parameter.
            if func.im_self:
                posargs.insert(0, func.im_self)
            # The first parameter must be the correct type.
            if 0:   # TODO: do we need to do this check?
                if not isinstance(posargs[0], func.im_class):
                    raise TypeError(
                        'unbound method %s() must be called with %s instance as first argument (got %s instance instead)' %
                        (func.im_func.func_name, func.im_class.__name__, type(posargs[0]).__name__)
                    )
            func = func.im_func
        if hasattr(func, 'func_code'):
            func_as_func = getattr(func, "func", func)
            callargs = inspect.getcallargs(func_as_func, *posargs, **namedargs)
            frame = self.make_frame(func.func_code, [], callargs)
            if func.func_code.co_flags & CO_GENERATOR:
                gen = Generator(frame, self)
                frame.generator = gen
                self.push(gen)
            else:
                self.push(self.run_frame(frame))
        else:
            self.push(func(*posargs, **namedargs))

    def byte_RETURN_VALUE(self):
        self.return_value = self.pop()
        if self.frame.generator:
            self.frame.generator.finished = True
        return "return"

    def byte_YIELD_VALUE(self):
        self.return_value = self.pop()
        return "yield"

    ## Importing

    def byte_IMPORT_NAME(self, name):
        fromlist = self.pop()
        level = self.pop()
        frame = self.frame
        self.push(__import__(name, frame.f_globals, frame.f_locals, fromlist, level))

    def byte_IMPORT_STAR(self):
        # TODO: this doesn't use __all__ properly.
        mod = self.pop()
        for attr in dir(mod):
            if attr[0] != '_':
                self.frame.f_locals[attr] = getattr(mod, attr)

    def byte_IMPORT_FROM(self, name):
        mod = self.peek()
        self.push(getattr(mod, name))

    ## And the rest...

    def byte_EXEC_STMT(self):
        one = self.pop()
        two = self.pop()
        three = self.pop()
        exec three in two, one

    def byte_BUILD_CLASS(self):
        methods = self.pop()
        bases = self.pop()
        name = self.pop()
        self.push(Class(name, bases, methods))

    if 0:   # Not in py2.7
        def byte_SET_LINENO(self, lineno):
            self.frame.f_lineno = lineno
