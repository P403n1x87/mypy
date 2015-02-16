"""Generator of dynamically typed stubs for arbitrary modules."""

import os.path

import mypy.parse
import mypy.traverser
from mypy.nodes import (
    IntExpr, UnaryExpr, StrExpr, BytesExpr, NameExpr, FloatExpr, MemberExpr, TupleExpr,
    ListExpr, ComparisonExpr, ARG_STAR, ARG_STAR2, ARG_NAMED
)


def generate_stub(path, output_dir):
    source = open(path).read()
    ast = mypy.parse.parse(source)
    gen = StubGenerator()
    ast.accept(gen)
    with open(os.path.join(output_dir, os.path.basename(path)), 'w') as file:
        file.write(''.join(gen.output()))


# What was generated previously.
EMPTY = 'EMPTY'
FUNC = 'FUNC'
CLASS = 'CLASS'
EMPTY_CLASS = 'EMPTY_CLASS'
VAR = 'VAR'


class StubGenerator(mypy.traverser.TraverserVisitor):
    def __init__(self):
        self._output = []
        self._import_lines = []
        self._imports = []
        self._indent = ''
        self._vars = [[]]
        self._state = EMPTY

    def visit_mypy_file(self, o):
        self._classes = find_classes(o)
        super().visit_mypy_file(o)

    def visit_func_def(self, o):
        if self.is_private_name(o.name()):
            return
        if not self._indent and self._state not in (EMPTY, FUNC):
            self.add('\n')
        self_inits = find_self_initializers(o)
        for init in self_inits:
            self.add_init(init)
        self.add("%sdef %s(" % (self._indent, o.name()))
        args = []
        for i, (arg, kind) in enumerate(zip(o.args, o.arg_kinds)):
            name = arg.name()
            init = o.init[i]
            if init:
                if kind == ARG_NAMED and '*' not in args:
                    args.append('*')
                arg = '%s=' % name
                init = init.rvalue
                if isinstance(init, IntExpr):
                    arg += str(init.value)
                elif isinstance(init, StrExpr):
                    arg += "''"
                elif isinstance(init, BytesExpr):
                    arg += "b''"
                elif isinstance(init, FloatExpr):
                    arg += "0.0"
                elif isinstance(init, UnaryExpr):
                    arg += '-%s' % init.expr.value
                elif isinstance(init, NameExpr) and init.name == 'None':
                    arg += init.name
                else:
                    self.add_import("Undefined")
                    arg += 'Undefined'
            elif kind == ARG_STAR:
                arg = '*%s' % name
            elif kind == ARG_STAR2:
                arg = '**%s' % name
            else:
                arg = name
            args.append(arg)
        self.add(', '.join(args))
        self.add("): pass\n")
        self._state = FUNC

    def visit_decorator(self, o):
        for decorator in o.decorators:
            if isinstance(decorator, NameExpr) and decorator.name in ('property',
                                                                      'staticmethod',
                                                                      'classmethod'):
                self.add('%s@%s\n' % (self._indent, decorator.name))
            elif (isinstance(decorator, MemberExpr) and decorator.name == 'setter' and
                  isinstance(decorator.expr, NameExpr)):
                self.add('%s@%s.setter\n' % (self._indent, decorator.expr.name))
        super().visit_decorator(o)

    def visit_class_def(self, o):
        if not self._indent and self._state != EMPTY:
            sep = len(self._output)
            self.add('\n')
        self.add('class %s' % o.name)
        base_types = []
        for base in o.base_type_exprs:
            if isinstance(base, NameExpr) and (base.name in self._classes or
                                               base.name.endswith('Exception') or
                                               base.name.endswith('Error')):
                base_types.append(base.name)
        if base_types:
            self.add('(%s)' % ', '.join(base_types))
        self.add(':\n')
        n = len(self._output)
        self._indent += '    '
        self._vars.append([])
        super().visit_class_def(o)
        self._indent = self._indent[:-4]
        self._vars.pop()
        if len(self._output) == n:
            if self._state == EMPTY_CLASS:
                self._output[sep] = ''
            self._output[-1] = self._output[-1][:-1] + ' pass\n'
            self._state = EMPTY_CLASS
        else:
            self._state = CLASS

    def visit_assignment_stmt(self, o):
        lvalue = o.lvalues[0]
        if isinstance(lvalue, (TupleExpr, ListExpr)):
            items = lvalue.items
        else:
            items = [lvalue]
        sep = False
        found = False
        for item in items:
            if isinstance(item, NameExpr):
                if not sep and not self._indent and self._state not in (EMPTY, VAR):
                    self.add('\n')
                    sep = True
                found = self.add_init(item.name) or found
        if found:
            self._state = VAR

    def visit_if_stmt(self, o):
        # Ignore if __name__ == '__main__'.
        expr = o.expr[0]
        if (isinstance(expr, ComparisonExpr) and
                isinstance(expr.operands[0], NameExpr) and
                isinstance(expr.operands[1], StrExpr) and
                expr.operands[0].name == '__name__' and
                '__main__' in expr.operands[1].value):
            return
        super().visit_if_stmt(o)

    def visit_import_all(self, o):
        self.add_import_line('from %s import *\n' % o.id)

    def add_init(self, lvalue):
        if lvalue in self._vars[-1]:
            return False
        if self.is_private_name(lvalue):
            return False
        self._vars[-1].append(lvalue)
        self.add('%s%s = Undefined(Any)\n' % (self._indent, lvalue))
        self.add_import('Undefined')
        self.add_import('Any')
        return True

    def add(self, string):
        self._output.append(string)

    def add_import(self, name):
        if name not in self._imports:
            self._imports.append(name)

    def add_import_line(self, line):
        self._import_lines.append(line)

    def output(self):
        imports = ''
        if self._imports:
            imports += 'from typing import %s\n' % ", ".join(self._imports)
        if self._import_lines:
            imports += ''.join(self._import_lines)
        if imports:
            imports += '\n'
        return imports + ''.join(self._output)

    def is_private_name(self, name):
        return name.startswith('_') and (not name.endswith('__')
                                         or name in ('__all__', '__author__', '__version__'))


def find_self_initializers(fdef):
    results = []
    class SelfTraverser(mypy.traverser.TraverserVisitor):
        def visit_assignment_stmt(self, o):
            lvalue = o.lvalues[0]
            if (isinstance(lvalue, MemberExpr) and
                    isinstance(lvalue.expr, NameExpr) and
                    lvalue.expr.name == 'self'):
                results.append(lvalue.name)
    fdef.accept(SelfTraverser())
    return results


def find_classes(cdef):
    results = set()
    class ClassTraverser(mypy.traverser.TraverserVisitor):
        def visit_class_def(self, o):
            results.add(o.name)
    cdef.accept(ClassTraverser())
    return results


if __name__ == '__main__':
    import sys
    for path in sys.argv[1:]:
        generate_stub(path, '.')
