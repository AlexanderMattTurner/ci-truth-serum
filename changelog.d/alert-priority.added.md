- `check-failure-notifier-coverage --require-alert-priority`: a workflow step
  invoking a local composite action that declares an OPTIONAL `priority:` input
  must pass one. Omitting it is not neutral — the call site silently inherits the
  urgency the action chose, which for an alert action is the maximum, and a repo
  whose every alert arrives at max urgency has trained its owner to swipe them
  all away, so the one that mattered is the one that gets ignored. The trigger is
  the invoked action's own declared inputs, never an action name, so a house
  alert composite is covered the day it grows a defaulted `priority`. Cosmetic
  inputs (`tags:`) are deliberately not required: priority is what drives phone
  behaviour, and demanding every optional input would make this noise rather than
  a severity discipline.
