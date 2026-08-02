# Shell lints parse the grammar, not the text

**When a lint's question is about shell _structure_, answer it with `_bash_ast`
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
of real shell wrong. `_bash_ast`'s own docstring records the first round of this:
two lints' hand-rolled quote/heredoc state machines "mis-parsed real shell", which
is why the module exists. `check-versionless-install` grew every one of the five
above and shipped four false positives in a day — help text read as a command, a
`;` inside quotes starting a new command, `>&2` read as a package name, an
interpreter name in a message counting as an executor — before being rewritten on
the grammar, after which the class disappeared rather than shrinking by one.

## Text scanning is still right for text

Use the line-oriented helpers (`_linecheck`) when the question is genuinely about
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

## The rule is not about bash

"Where is the comment" is the same structural question in every language, and a
delimiter scan gets each one wrong in its own way. All four lints that read
narration — `check_drift_guards`, `check_graceful_handwave`,
`check_historical_comments`, `check_workflow_refs` — now ask `_comments`, which
picks the parser the PATH names:

| language | the parser  | what the text scan got wrong                                                        |
| -------- | ----------- | ----------------------------------------------------------------------------------- |
| Python   | `tokenize`  | a `#` in a string literal — and an opt-out token there SUPPRESSED, failing open     |
| shell    | `_bash_ast` | a heredoc body read as a run of comments                                            |
| JS/TS    | `_js_ast`   | a `//` inside a string or template literal; a `/* … */` after code on the same line |
| YAML     | none        | nothing — its parsers discard comments, so the delimiter scan is the decision       |

Nor is it only about comments. The lints that read PYTHON ask the same shape of
structural question, and answered it the same wrong way until they were moved onto
`_py_ast` (stdlib `ast`, no new dependency):

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
