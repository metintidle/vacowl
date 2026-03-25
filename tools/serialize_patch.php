#!/usr/bin/env php
<?php
/**
 * Batch worker: apply ordered string replacements inside PHP-serialized values safely.
 *
 * Usage:
 *   php serialize_patch.php <replacements.json>
 *
 * replacements.json: [["from","to"], ...] applied in order (longest-match safety: put longer keys first).
 *
 * Stdin: one JSON object per line: {"value":"<meta_value or any string>"}
 * Stdout: {"value":"<patched>"} or {"error":"..."}
 *
 * One-shot test:
 *   echo '{"value":"a:0:{}"}' | php serialize_patch.php repl.json
 */
declare(strict_types=1);

if ($argc < 2) {
    fwrite(STDERR, "Usage: php serialize_patch.php <replacements.json>\n");
    exit(2);
}

$replPath = $argv[1];
if (!is_readable($replPath)) {
    fwrite(STDERR, "Cannot read replacements file: {$replPath}\n");
    exit(2);
}

$pairs = json_decode((string) file_get_contents($replPath), true);
if (!is_array($pairs)) {
    fwrite(STDERR, "replacements.json must be a JSON array of [from,to] pairs\n");
    exit(2);
}
foreach ($pairs as $i => $p) {
    if (!is_array($p) || count($p) !== 2 || !is_string($p[0]) || !is_string($p[1])) {
        fwrite(STDERR, "Invalid pair at index {$i}: expected [string, string]\n");
        exit(2);
    }
}

while (($line = fgets(STDIN)) !== false) {
    $line = trim($line);
    if ($line === '') {
        continue;
    }
    $row = json_decode($line, true);
    if (!is_array($row) || !array_key_exists('value', $row)) {
        echo json_encode(['error' => 'expected JSON line with "value" key'], JSON_UNESCAPED_UNICODE) . "\n";
        fflush(STDOUT);
        continue;
    }
    $value = $row['value'];
    if ($value === null) {
        echo json_encode(['value' => null], JSON_UNESCAPED_UNICODE) . "\n";
        fflush(STDOUT);
        continue;
    }
    if (!is_string($value)) {
        echo json_encode(['error' => '"value" must be string or null'], JSON_UNESCAPED_UNICODE) . "\n";
        fflush(STDOUT);
        continue;
    }
    try {
        $out = patch_meta_value($value, $pairs);
        $flags = JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES;
        if (defined('JSON_INVALID_UTF8_SUBSTITUTE')) {
            $flags |= JSON_INVALID_UTF8_SUBSTITUTE;
        }
        echo json_encode(['value' => $out], $flags) . "\n";
        fflush(STDOUT);
    } catch (Throwable $e) {
        echo json_encode(['error' => $e->getMessage()], JSON_UNESCAPED_UNICODE) . "\n";
        fflush(STDOUT);
    }
}

/**
 * @param list<array{0:string,1:string}> $pairs
 */
function patch_meta_value(string $raw, array $pairs): string
{
    if ($raw === '') {
        return $raw;
    }
    if (!is_probably_serialized($raw)) {
        return patch_plain_string($raw, $pairs);
    }
    $un = @unserialize($raw, ['allowed_classes' => true]);
    if ($un === false && !is_false_serialized($raw)) {
        return patch_plain_string($raw, $pairs);
    }
    $patched = patch_recursive($un, $pairs);
    return serialize($patched);
}

function is_false_serialized(string $data): bool
{
    $t = trim($data);
    return $t === 'b:0;';
}

/**
 * WordPress-style serialized detection (trimmed).
 */
function is_probably_serialized(string $data, bool $strict = true): bool
{
    if ($data === '') {
        return false;
    }
    $data = trim($data);
    if ($data === 'N;') {
        return true;
    }
    if (strlen($data) < 4) {
        return false;
    }
    if ($data[1] !== ':') {
        return false;
    }
    if ($strict) {
        $lastc = substr($data, -1);
        if ($lastc !== ';' && $lastc !== '}') {
            return false;
        }
    }
    $token = $data[0];
    switch ($token) {
        case 's':
            if ($strict) {
                if (substr($data, -2, 1) !== '"') {
                    return false;
                }
            } elseif (strrpos($data, '"') === false) {
                return false;
            }
            // fall through — preg_match uses $token (still "s")
        case 'a':
        case 'O':
            return (bool) preg_match("/^{$token}:[0-9]+:/s", $data);
        case 'b':
        case 'i':
        case 'd':
            $end = $strict ? '$' : '';
            return (bool) preg_match("/^{$token}:[0-9.E+-]+;$end/", $data);
    }
    return false;
}

/**
 * @param mixed $data
 * @param list<array{0:string,1:string}> $pairs
 * @return mixed
 */
function patch_recursive($data, array $pairs)
{
    if (is_string($data)) {
        return patch_plain_string($data, $pairs);
    }
    if (is_array($data)) {
        $out = [];
        foreach ($data as $k => $v) {
            $out[$k] = patch_recursive($v, $pairs);
        }
        return $out;
    }
    if (is_object($data)) {
        if ($data instanceof __PHP_Incomplete_Class) {
            return $data;
        }
        foreach (get_object_vars($data) as $k => $v) {
            $data->$k = patch_recursive($v, $pairs);
        }
        return $data;
    }
    return $data;
}

/**
 * @param list<array{0:string,1:string}> $pairs
 */
function patch_plain_string(string $s, array $pairs): string
{
    foreach ($pairs as [$from, $to]) {
        if ($from === '') {
            continue;
        }
        $s = str_replace($from, $to, $s);
    }
    return $s;
}
