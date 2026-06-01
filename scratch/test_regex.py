"""Reproduce the exact regex bug in extract_metrics_and_plots_paths."""
import re

# Simulate what YAML gives us - the raw string with ${item}
p = "results/metrics/recbole_metrics_EASE_" + "$" + "{item}.json"
print(f"Input string: {p!r}")
print(f"Has dollar-brace: {'${' in p}")

# Step 1: re.escape
escaped = re.escape(p)
print(f"After re.escape: {escaped!r}")

# Step 2: The re.sub pattern from the code (line 1398)
sub_pattern = r'\\\$\\\{[^}]+\\\}'
print(f"Sub pattern: {sub_pattern!r}")
result = re.sub(sub_pattern, '.*', escaped)
print(f"After re.sub: {result!r}")

# Step 3: Compile
try:
    final = re.compile(f"^{result}$")
    print(f"Final pattern: {final.pattern}")
    
    target = "results/metrics/recbole_metrics_EASE_tomplay.json"
    m = final.match(target)
    print(f"Match '{target}': {m}")
except Exception as e:
    print(f"Compile error: {e}")

print()
print("=" * 60)
print("DEBUGGING re.escape behavior:")
print("=" * 60)

# What does re.escape actually do with $?
for char in ['$', '{', '}', '.']:
    print(f"  re.escape('{char}') = {re.escape(char)!r}")

dollar_brace = "$" + "{item}"
print(f"  re.escape('${{item}}') = {re.escape(dollar_brace)!r}")

full = "abc_" + "$" + "{item}.json"
escaped_full = re.escape(full)
print(f"  re.escape(full) = {escaped_full!r}")

# Now try to find the right sub pattern
print()
print("=" * 60)
print("FINDING THE RIGHT PATTERN:")
print("=" * 60)

# In Python 3.7+, re.escape only escapes special regex chars
# $ is a regex special char, { and } are NOT special in basic regex
# So re.escape("${item}") should give us "\\$\\{item\\}" ... no, let's check

import sys
print(f"Python version: {sys.version}")

test = "$" + "{item}"
escaped_test = re.escape(test)
print(f"re.escape('${{item}}') = {escaped_test!r}")
# In Python 3.7+: re.escape escapes only chars that have meaning in regex
# $ -> \$
# { -> \{ (since Python 3.7.2 curly braces are escaped)  
# } -> \}
# So result should be: \$\{item\}

# The correct sub pattern should match \$\{...\} in the escaped string
# In a raw string, we need: r'\\\$\\\{[^}]+\\\}'
# \\ matches a literal \
# \$ matches a literal $
# etc.
correct_pattern = r'\\\$\\\{[^}]+\\\}'
print(f"Correct sub pattern: {correct_pattern!r}")
result2 = re.sub(correct_pattern, '.*', escaped_full)
print(f"After correct sub: {result2!r}")

# Also try simpler approach
simple_pattern = r'\\\$\{[^}]+\}'
result3 = re.sub(simple_pattern, '.*', escaped_full)
print(f"After simple sub: {result3!r}")

# Even simpler - work on original string BEFORE escaping
print()
print("=" * 60)
print("ALTERNATIVE: Replace before escaping")
print("=" * 60)

# Replace ${...} with a placeholder, then escape, then restore
import uuid
placeholder = "__WILDCARD__"
original = "results/metrics/recbole_metrics_EASE_" + "$" + "{item}.json"
with_placeholder = re.sub(r'\$\{[^}]+\}', placeholder, original)
print(f"With placeholder: {with_placeholder!r}")
escaped_ph = re.escape(with_placeholder)
print(f"Escaped: {escaped_ph!r}")
final_pattern = escaped_ph.replace(placeholder, '.*')
print(f"Final: {final_pattern!r}")
compiled = re.compile(f"^{final_pattern}$")
print(f"Pattern: {compiled.pattern}")
print(f"Match: {compiled.match('results/metrics/recbole_metrics_EASE_tomplay.json')}")
