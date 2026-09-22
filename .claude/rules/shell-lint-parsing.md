# Shell lints parse the grammar, not the text

**When a lint's question is about shell _structure_, answer it with `_cts_bash_ast`
(tree-sitter-bash). Never with a regex, a quote-state scanner, or `shlex`.**

Structural questions — the ones a text scan cannot answer, only approximate:

| Question                                     | The node that answers it                                 |
| -------------------------------------------- | -------------------------------------------------------- |
| Is this a command, or text a command prints? | `command` vs a `string` argument                         |
| Is this token an argument or a redirection?  | `word` vs `file_redirect` (a **sibling** of the command) |
| Is this one command or two?                  | `list` / `pipeline` children                             |
| Does this `;` separate commands?             | it does not, inside a `string`                           |
| Is this body executed?                       | `heredoc_body` is data; a shell's `-c` argument is not   |
| Is this value fixed or computed?             | `expansion` / `command_substitution` child               |
| Where does this command start and end?       | `start_point` / `end_point`, continuations included      |

## The tell

You are on the wrong side of this rule the moment you write any of these:

- a character walk tracking whether you are inside `'` or `"`;
- a "find the previous `&&`/`;`/`|`" segment splitter;
- `shlex.split` to recover a command's arguments;
- a regex asserting a command boundary (`(?:^|[\s;&|(])cmd`);
- a filter dropping `>&2`-shaped tokens from an argument list.

Each is a re-implementation of the grammar, and each one gets a different subset
of real shell wrong. `_cts_bash_ast`'s own docstring records the first round of this:
two lints' hand-rolled quote/heredoc state machines "mis-parsed real shell", which
is why the module exists. `check-versionless-install` grew every one of the five
above and shipped four false positives in a day — help text read as a command, a
`;` inside quotes starting a new command, `>&2` read as a package name, an
interpreter name in a message counting as an executor — before being rewritten on
the grammar, after which the class disappeared rather than shrinking by one.

## Text scanning is still right for text

Use the line-oriented helpers (`_cts_linecheck`) when the question is genuinely about
text rather than structure: reading an `# annotation:` out of a comment, judging
prose in a doc, matching a version string's shape, or scanning a file format that
is not shell at all. `strip_comments` is not a substitute for parsing — it uses
the grammar to blank comments and then hands you text.

## Cost, and the one carve-out

`parse()` is not free: it refuses pathological input (`PathologicalInputError`,
which a lint must surface **loudly** — see `check_untrusted_exec.main`) and costs
a parse per file. That is the price of being right about shell, and every lint in
this pack already pays it through the aggregate. A text scan is only justified
when the input is not shell; say so in the module docstring when you take that
route, so the next reader knows it was a decision.

## Audit — the two probes every shell lint should survive

Measured by feeding each one its own banned idiom twice: once inside a logger's
message string (`gb_warn "…"`), once inside a heredoc body written to a file.
Neither is executed code, so a finding is a false positive:

| Lint                              | Fires on a message string | Fires on a heredoc body |
| --------------------------------- | ------------------------- | ----------------------- |
| `check_exit_suppression`          | no                        | no                      |
| `check_echo_fallback`             | no                        | no                      |
| `check_stderr_merge_parse`        | no                        | no                      |
| `check_pinned_downloads`          | no                        | no                      |
| `check_gh_slurp_jq`               | no                        | no                      |
| `check_stderr_suppression`        | no                        | no                      |
| `check_substitution_exit_swallow` | no                        | no                      |
| `check_secret_file_perms`         | no                        | no                      |
| `check_drift_guards`              | no                        | no                      |
| `check_argument_exit_swallow`     | no                        | no                      |
| `check_soft_timeout`              | no                        | no                      |
| `check_curl_retry`                | no                        | no                      |

The top five fired on one or both probes and were rewritten on the grammar, which
removed the class rather than the instance. **Every lint in the table now parses**;
the bottom three were the last text scanners, and passing both probes was never
the same as being structurally sound — each was answering a structural question by
approximation:

| Lint                              | The approximation it dropped                                                                                      |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `check_substitution_exit_swallow` | `[^\|;&]*` standing in for "inside one pipeline segment" — which is what a `pipeline` node **is**                 |
| `check_stderr_suppression`        | co-occurring `>/dev/null` + `2>&1` tokens, order-blind; and `(?<![-\w])build` to tell a subcommand from `--build` |
| `check_secret_file_perms`         | "~3 non-blank lines" standing in for the next few statements, and a hand-rolled redirect/comment scanner          |

Two verdicts CHANGED, both toward the grammar's answer, and both are pinned by a
new test: `2>&1 >/dev/null` no longer counts as suppression (bash dups stderr onto
the still-live stdout, then moves only stdout), and a launch or a producer after a
logging call on the same line is now judged — the old `MESSAGE_PREFIX` skip excused
the whole line because its FIRST word printed something.

`check_drift_guards` is the ninth row and a later, separate instance of the same
story: its laundered-copy trigger shipped reading comments out of the text, fired
on the heredoc probe the first time the probe was run against it, and was moved
onto the bash grammar before landing. Note which half moved — only "where does a
comment start", the structural question. Judging the PROSE inside that comment
stays a regex, because English has no grammar here to parse; that is the
carve-out above, not a second thing to fix.

`check_argument_exit_swallow` is the tenth row and the first one that was written
on the grammar from the start. It asks three structural questions, and a text
scan answers each one wrong: is this substitution an ARGUMENT or the right-hand
side of an assignment (`command` child versus `variable_assignment` child — the
whole line between this rule and SC2155); is this word the command's NAME or an
argument (`"$(get_tool)" --flag` is a computed program, not a swallowed
argument); and is this call executed at all (a call quoted inside a `gb_warn`
message, or written into a heredoc body, is text). Both probes were run against
it before it landed, and its suite pins both verdicts.

`check_soft_timeout` is the eleventh row and the second one written on the grammar
from the start. It asks three structural questions. Is this word a command, or a
word inside a sentence a command prints (`gb_warn "raise the timeout 60 seconds"`
holds no words at all, and a printing command's arguments are text)? Is this
`timeout` the command word, or an argument of another launcher
(`sbx exec box -- timeout 60 cmd` bounds a program the sandbox starts)? Is this
token an argument or a redirection (`timeout 60 cmd >&2` — a word scan that kept
the `>&2` would read it as the bounded command)? A fourth shape is the reason the
lint exists: `run=(timeout 600)` is a `variable_assignment` whose value is an
`array`, so there is no command named `timeout` in the tree. A sweep of
`agent-glovebox` that searched for the COMMAND found 22 sites and missed 9, and
two of the nine were live defects — a hung package install and a stranded kill
switch. Both probes were run against it before it landed, and its suite pins both
verdicts.

`check_curl_retry` is the twelfth row and was written on the grammar from the
start. It began with the usual pair. Is this `curl` a command, or a word in a
sentence a command prints? Where does this command start and end, so that a
backslash-continued download is one command with an `-o` three lines down?

Its second arm added four more, and each one is a place a word scan reports a
clean verdict on a download that has no working retry:

| The structural question                           | The node that answers it                                                     |
| ------------------------------------------------- | ---------------------------------------------------------------------------- |
| Is this flag written here, or held in a variable? | a `variable_assignment`, and the literal pieces of its `value`               |
| Does that assignment reach this call?             | `start_byte`, compared against the command's                                 |
| Is this expansion the WHOLE argument?             | an `expansion` argument, versus one child of a `string` or a `concatenation` |
| Is this value literal, or computed?               | a `command_substitution` child makes the words unknown                       |

The grammar answers each one. A word scan of the curl line alone reads
`retry_widen="--retry-connrefused"` plus `curl … "$retry_widen" -o f` as a
defect, and two real downloads in `agent-glovebox` have that shape. Reading the
words as an unordered SET fails the other way: `curl … "https://e/$retry/$wide"`
holds both names inside a URL, so curl gets an address and no flag, and a set of
the words says the call is retried. Source order is the approximation the check
keeps, and it is the conservative one: dominance analysis would say which
assignments actually run before the call, and the weaker question only ever
credits fewer flags. Both probes were run against the check before it landed,
and its suite pins both verdicts under each arm.

## The rule is not about bash

"Where is the comment" is the same structural question in every language, and a
delimiter scan gets each one wrong in its own way. All four lints that read
narration — `check_drift_guards`, `check_graceful_handwave`,
`check_historical_comments`, `check_workflow_refs` — now ask `_cts_comments`, which
picks the parser the PATH names:

| language | the parser                                                   | what the text scan got wrong                                                                         |
| -------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Python   | `tokenize`                                                   | a `#` in a string literal — and an opt-out token there SUPPRESSED, failing open                      |
| shell    | `_cts_bash_ast`                                              | a heredoc body read as a run of comments                                                             |
| JS/TS    | `_cts_js_ast`                                                | a `//` inside a string or template literal; a `/* … */` after code on the same line                  |
| YAML     | PyYAML's scanner, plus `_cts_bash_ast` inside a `run:` value | a `#` inside a quoted scalar read as a comment — and an opt-out token there SUPPRESSED, failing open |

## YAML: the parser discards the answer, so invert the scanner

A YAML parser drops every comment before it hands you a document, so there is no
comment node to walk. That is why the pack read YAML comments by delimiter for
so long, and why 26 checks shipped the same fail-open. PyYAML's SCANNER still
reports where each scalar starts and ends, and the bytes no scalar covers are
the comments. `_cts_linecheck.yaml_comment_view` is that inversion: it blanks
every scalar and keeps each comment at its own line and column.

The fail-open it closes is one line of YAML. A workflow's own author writes
every value in it, including this one:

    name: "deploy  # allow-no-timeout: not really"

Read as text, that line carries a reason-bearing opt-out, so the lint disarms
itself and the job needs no `timeout-minutes`. Read through the scanner, the
whole span is one scalar and the marker is part of a job's name. A sweep of the
pack found 26 checks matching an opt-out against raw lines, and every one of
them honoured that value.

A workflow holds a SECOND language, and the two views differ only in whether they keep it. A `run:` value is a shell script, where a `#` does open a real comment, and `yaml_comment_view` blanks it with every other value. `yaml_script_view` adds those values back, so it is the surface rule for an opt-out: **a marker sits in a real YAML comment, or in a `run:` value.** Every opt-out in the pack takes `yaml_script_view`. There is no per-check exception, and that is the point — "which view does this check take" was a free variable, and a free variable drifts.

Both views err CLOSED. A file PyYAML cannot scan yields an all-blank view, so every suppression is lost and the finding stands. That is the safe direction for an opt-out, and it is the opposite of what the delimiter scan did.

### Two questions, and only one of them is the view's

Ask which SURFACE may carry a marker, and ask which findings that marker clears. They are independent, and the view answers only the first. The scope is each check's own contract, and a check states it by the slice of text it passes in: `check_job_timeout` passes one job's block, `check_trusted_base` passes the whole file. Never reach for the narrower view to narrow a scope. A file-scoped marker is wide because the check reads the whole file, and a real YAML comment at the top of that file is exactly as wide as a script comment inside it.

### The one carve-out: a marker that DECLARES

`check_path_gate_deps` reads two markers out of the same job block, and only one of them is an opt-out.

- `# path-gate-ok: <dep> <reason>` REMOVES a finding. It takes `yaml_script_view`, like every other opt-out.
- `# gate-deps: <path>` ADDS a path the check then proves is covered. It takes `yaml_comment_view`.

A job's script prints and greps paths all day. `echo "# gate-deps: src/"` is a line a step outputs, not a declaration its author wrote, and reading it turns a real gap into a false green — the exact failure this lint exists to catch. So the rule sits on the marker's KIND rather than its scope: **a marker that declares a value is read only from real YAML comments.**

### Block STYLE is not "this is a script"

The first spelling of `yaml_script_view` asked whether a scalar was written in block style (`|` or `>`). Style is a proxy for "GitHub hands this to a shell", and it is wrong in both directions. Measured:

| the line                                    | the truth                 | what the style test did |
| ------------------------------------------- | ------------------------- | ----------------------- |
| `run: 'git diff  # frozen-head-ok: lease'`  | a script, not block style | lost the marker         |
| `description: \|` with a marker in the body | block style, not a script | read the marker         |
| `if: >` with a marker in the body           | block style, not a script | read the marker         |

The second and third are the same fail-open the section above closes, one level down: a `description:` body is prose an author writes, and a marker read out of it suppresses a real finding.

The KEY answers the question the style only approximated. PyYAML's scanner names the key each scalar belongs to (`KeyToken` → `ScalarToken` → `ValueToken` → `ScalarToken`), so `yaml_run_scalars` selects the values of `run:` in any style. A `defaults.run:` holds a MAPPING, so it yields no scalar and needs no special case.

Two smaller traps come with the token span, and both cost a real marker:

- The span covers the whole value TOKEN, so a block scalar's opens at the `|` and a quoted scalar's carries its quotes. Hand that to bash and `run: 'git diff  # why'` parses as ONE STRING with no comment in it. `yaml_run_script` blanks the YAML and keeps every offset.
- That same block-scalar span reaches back over a real comment written after the indicator, so the inversion blanked the marker in `description: | # unused-input-ok: <reason>`. `yaml_comment_view` re-exposes the header line's comment. The grammar allows only the indicator, then chomping and indentation indicators, then blanks, then a comment — so the first `#` on that line opens one and nothing else can.

### The third reader: what a line SAYS

`_cts_comments.yaml_comments` answers a different question: not "may a marker sit here" but "what does this line say". It unions the comment view with `shell_comments` run over each `run:` value, so the bash grammar judges the script's `#` and PyYAML judges the workflow's. Measured on one 10-line workflow, the delimiter scan it replaced claimed two lines that are not comments: a quoted `name:` value, and `echo "quoted # not a comment"` inside a `run:` body.

It errs the OTHER way from the views, and the difference is not an inconsistency — it follows from what each one is read for. An all-blank view drops a suppression and the finding stands, which is safe. An empty comment map says a malformed workflow cites nothing at all, which is the false green. So `yaml_comments` falls back to `text_comments` when the scan fails, exactly as `comment_lines` does for Python that will not tokenize. The cost is that a `#` inside a quoted scalar reads as a comment again — on a file that does not parse, and only until it does.

Nor is it only about comments. The lints that read PYTHON ask the same shape of
structural question, and answered it the same wrong way until they were moved onto
`_cts_py_ast` (stdlib `ast`, no new dependency):

| Lint                      | The structural question                   | What the text scan got wrong                                                                                           |
| ------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `check_global_stdio_swap` | is this name an assignment TARGET?        | a swap inside a string literal — it flagged its own docstring and 12 of its own fixtures                               |
| `check_toolchain_skips`   | where does this call's argument list end? | a balanced-paren walk with its own quote state; and a `reason=` string counting as both the discovery and the CI guard |

Between them that was 23 findings on this repo's tracked tree, every one a false
positive, and both had been muted in `.pre-commit-config.yaml` because of it — the
quiet cost of a text scan is not the noise, it is the check being switched off.

Measured over 241 real `.mjs`/`.js`/`.ts` files in `agent-glovebox`, the JS
delimiter scan claimed 169 lines that are not comments and missed 250 that are.
Pick the JS/TS grammar by the path's suffix, never by sniffing the content: a
`.ts` file parsed as JavaScript is a tree of ERROR nodes from its first type
annotation on, and every comment after that is lost.

Reproduce with `violations()` on `gb_warn "<idiom>"` and on a
`cat <<'EOF' > doc.txt` block containing the idiom — that pair is the cheapest
audit of a shell lint you will ever run, and both cases are text no shell
executes.

A rewrite has to preserve every verdict its existing suite pins: that is the
evidence the rule was kept and only the decision procedure changed. State in the
commit message how many assertions moved and why — for these five it was one line
number in `check_gh_slurp_jq`, one changed meta-test shape in
`check_exit_suppression`, and zero in the other three.
