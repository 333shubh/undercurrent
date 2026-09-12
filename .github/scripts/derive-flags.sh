#!/usr/bin/env bash
#
# Derive the two query flags for POST /trigger from the workflow inputs.
#
# This exists as a script rather than a `${{ }}` expression because the inline
# version silently broke the product. It read:
#
#     DELIVER="${{ inputs.deliver == false && '0' || '1' }}"
#
# On a `workflow_dispatch` that is correct. On a `schedule` there are no inputs,
# so `inputs.deliver` is null -- and GitHub's `==` coerces both null and false
# to 0 before comparing, which makes `null == false` *true*. Every scheduled run
# therefore posted `deliver=0`: the pipeline woke up, spent the LLM budget,
# wrote its run_log, and deliberately posted nothing. The workflow went green
# each morning while the digest never arrived.
#
# Shell string comparison has no such coercion. An absent input is the empty
# string, which is neither "true" nor "false", so both flags fall back to their
# scheduled-run defaults: deliver, and do not force.
#
# Inputs come from the environment (FORCE_INPUT, DELIVER_INPUT) so nothing
# interpolates workflow values into the script body. Results go to stdout as
# `name=value` lines, which is also $GITHUB_OUTPUT's format.

set -euo pipefail

# Default: a scheduled run delivers. Only an explicit "false" from a manual
# dispatch turns delivery off -- an unset, empty or unrecognised value must
# never be read as "dry run", because a silent dry run looks like success.
deliver=1
if [ "${DELIVER_INPUT:-}" = "false" ]; then
  deliver=0
fi

# Default: do not force. Regenerating over an already-delivered digest is the
# exceptional case, so anything other than an explicit "true" leaves it off.
force=0
if [ "${FORCE_INPUT:-}" = "true" ]; then
  force=1
fi

echo "force=${force}"
echo "deliver=${deliver}"
