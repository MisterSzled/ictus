# Wiring and conditions

Read this when connecting nodes: which of `connect`, `route`, `feed` and
`connect_input` to use, and how to write a route condition that is actually
true at run time.

## Wiring

| Call | Carries | Use when |
| --- | --- | --- |
| `pipeline.connect(src, port, dst, port, when=)` | control + data | the target runs next and reads the value |
| `pipeline.route(src, dst, when=)` | control only | the target needs nothing from the source |
| `pipeline.feed(src, port, dst, port)` | data only | the value crosses a gate or a branch |
| `pipeline.connect_input(param, dst, port)` | a workflow input | binding the pipeline's own parameters |

## Conditions

Conditions are built from references, so they stay correct when what they were
derived from changes.

`equals` refuses a value whose Python type does not match the port's, because
the condition it would render is well-formed and never true. A route is tested
against the value the engine stored, not its rendered text, so `exit_code` is a
real `0` and a verdict is a real `True` — `== '0'` and `== 'true'` both send
every run down the catch-all with nothing to see. An `ARRAY` or `OBJECT` port
cannot be compared at all; route on a scalar the step also declares.

| Helper | Renders |
| --- | --- |
| `equals(ref, value)` / `not_equals` | `{{ x == 'value' }}` for a string, `{{ x \| int == 0 }}` for an int, `{{ x == true }}` for a bool — the value's type must match the port's |
| `every(*refs)` / `not_every` | `{{ a and b and c }}`, negated as `{{ not (a and b and c) }}` — true while *any* is false |
| `at_least(ref, n)` | `{{ x \| int >= n }}` |
| `tpl(...)`, `optional(...)`, `ref_to(id, port, type)` | prompt text with typed references; `when=tpl(ref)` renders `{{ x }}`, the usual boolean route |
