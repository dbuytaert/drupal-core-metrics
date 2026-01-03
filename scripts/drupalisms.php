#!/usr/bin/env php
<?php
/**
 * @file
 * Analyze Drupal codebase for anti-patterns and API surface area.
 *
 * Two separate metrics:
 *
 * 1. ANTI-PATTERNS (per-file, density metric - should decrease)
 *    - Service Locator: Static \Drupal:: calls that bypass dependency injection
 *    - Deep Arrays: Complex nested array structures (render arrays, configs)
 *    - Magic Keys: #-prefixed array keys that require memorization
 *
 * 2. API SURFACE AREA (codebase-level, count metric)
 *    - Plugin types: Distinct plugin systems (Block, Field, Action, etc.)
 *    - Hooks: Distinct hook names (form_alter, entity_presave, etc.)
 *    - Magic keys: Distinct #-prefixed render array keys
 *    - Events: Distinct Symfony events subscribed to
 *    - Services: Distinct service types from *.services.yml
 *    - YAML formats: Distinct YAML extension point formats (routing, permissions, etc.)
 *
 * Usage: php drupalisms.php /path/to/drupal/core
 * Output: JSON with anti-patterns, surface area, and file-level metrics
 */

require_once __DIR__ . '/../vendor/autoload.php';

use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\ParserFactory;
use PhpParser\Node;

/*
 * =============================================================================
 * CONFIGURATION
 * =============================================================================
 */

/**
 * Keys to ignore when counting magic keys.
 * These are common data keys that aren't "magic" - they don't trigger
 * special behavior, they're just standard form/render element properties.
 */
const IGNORED_KEYS = [
    '#type', '#markup', '#title', '#weight', '#prefix', '#suffix',
    '#attributes', '#default_value', '#description', '#required',
    '#options', '#rows', '#cols', '#size', '#maxlength', '#placeholder',
    '#cache', '#attached', '#value', '#name', '#id', '#disabled',
    '#checked', '#selected', '#min', '#max', '#step', '#pattern',
    '#autocomplete', '#multiple', '#empty_option', '#empty_value',
];

/**
 * Anti-pattern weights.
 */
const SERVICE_LOCATOR_WEIGHT = 1;

/*
 * =============================================================================
 * FILE METRICS TRACKER
 * =============================================================================
 */

/**
 * Centralized tracker for per-file metrics.
 */
class FileMetricsTracker
{
    private array $files = [];

    public function initFile(string $file, bool $isTest, int $loc): void
    {
        $this->files[$file] = [
            'file' => $file,
            'test' => $isTest,
            'loc' => $loc,
            'ccn' => 1,
            'mi' => 100,
            'antipatterns' => 0,
        ];
    }

    public function addCcn(string $file, int $points): void
    {
        if (isset($this->files[$file])) {
            $this->files[$file]['ccn'] += $points;
        }
    }

    public function addAntipatterns(string $file, int $score): void
    {
        if (isset($this->files[$file])) {
            $this->files[$file]['antipatterns'] += $score;
        }
    }

    public function calculateMi(string $file): void
    {
        if (!isset($this->files[$file])) {
            return;
        }
        $f = &$this->files[$file];
        $loc = max($f['loc'], 1);
        $ccn = max($f['ccn'], 1);

        $volume = $loc * 5;
        $mi = 171 - 5.2 * log($volume) - 0.23 * $ccn - 16.2 * log($loc);
        $f['mi'] = (int) max(0, min(100, $mi));
    }

    public function getFiles(): array
    {
        return array_values($this->files);
    }
}

/**
 * Tracks anti-pattern occurrence counts by category.
 * Note: Test file filtering happens at traversal level, not here.
 */
class AntipatternTracker
{
    private FileMetricsTracker $metrics;
    private int $serviceLocators = 0;
    private int $deepArrays = 0;
    private int $magicKeys = 0;

    public function __construct(FileMetricsTracker $metrics)
    {
        $this->metrics = $metrics;
    }

    public function addServiceLocators(string $file, int $score): void
    {
        $this->serviceLocators += $score;
        $this->metrics->addAntipatterns($file, $score);
    }

    public function addDeepArrays(string $file, int $score): void
    {
        $this->deepArrays += $score;
        $this->metrics->addAntipatterns($file, $score);
    }

    public function addMagicKeys(string $file, int $score): void
    {
        $this->magicKeys += $score;
        $this->metrics->addAntipatterns($file, $score);
    }

    public function getCounts(): array
    {
        return [
            'magicKeys' => $this->magicKeys,
            'deepArrays' => $this->deepArrays,
            'serviceLocators' => $this->serviceLocators,
        ];
    }
}

/*
 * =============================================================================
 * SURFACE AREA COLLECTOR
 * =============================================================================
 */

/**
 * Collects distinct API surface area types across the codebase.
 */
class SurfaceAreaCollector
{
    public array $pluginTypes = [];
    public array $hooks = [];
    public array $magicKeys = [];
    public array $events = [];
    public array $services = [];
    public array $yamlFormats = [];
    public array $interfaceMethods = [];

    public function addPluginType(string $name): void
    {
        $this->pluginTypes[$name] = true;
    }

    public function addHook(string $pattern): void
    {
        $this->hooks[$pattern] = true;
    }

    public function addMagicKey(string $key): void
    {
        $this->magicKeys[$key] = true;
    }

    public function addEvent(string $event): void
    {
        $this->events[$event] = true;
    }

    public function addService(string $name): void
    {
        $this->services[$name] = true;
    }

    public function addYamlFormat(string $format): void
    {
        $this->yamlFormats[$format] = true;
    }

    public function addInterfaceMethod(string $interfaceMethod): void
    {
        $this->interfaceMethods[$interfaceMethod] = true;
    }

    public function getCounts(): array
    {
        return [
            'pluginTypes' => count($this->pluginTypes),
            'hooks' => count($this->hooks),
            'magicKeys' => count($this->magicKeys),
            'events' => count($this->events),
            'services' => count($this->services),
            'yamlFormats' => count($this->yamlFormats),
            'interfaceMethods' => count($this->interfaceMethods),
        ];
    }

    public function getLists(): array
    {
        return [
            'pluginTypes' => array_keys($this->pluginTypes),
            'hooks' => array_keys($this->hooks),
            'magicKeys' => array_keys($this->magicKeys),
            'events' => array_keys($this->events),
            'services' => array_keys($this->services),
            'yamlFormats' => array_keys($this->yamlFormats),
            'interfaceMethods' => array_keys($this->interfaceMethods),
        ];
    }
}

/*
 * =============================================================================
 * HELPER FUNCTIONS
 * =============================================================================
 */

function isTestFile(string $path): bool
{
    return str_starts_with($path, 'tests/')
        || str_contains($path, '/tests/')
        || str_contains($path, '/Tests/')
        || str_ends_with($path, 'Test.php')
        || str_ends_with($path, 'TestBase.php');
}

function countLinesOfCode(string $code): int
{
    $lines = explode("\n", $code);
    $count = 0;
    $inBlockComment = false;

    foreach ($lines as $line) {
        $trimmed = trim($line);
        if ($trimmed === '') continue;
        if ($inBlockComment) {
            if (str_contains($trimmed, '*/')) $inBlockComment = false;
            continue;
        }
        if (str_starts_with($trimmed, '//') || str_starts_with($trimmed, '#')) continue;
        if (str_starts_with($trimmed, '/*') || str_starts_with($trimmed, '/**')) {
            if (!str_contains($trimmed, '*/')) $inBlockComment = true;
            continue;
        }
        if (str_starts_with($trimmed, '*')) continue;
        $count++;
    }
    return $count;
}

function findPhpFiles(string $directory): array
{
    $files = [];
    $extensions = ['php', 'module', 'inc', 'install', 'theme', 'profile', 'engine'];

    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($directory, RecursiveDirectoryIterator::SKIP_DOTS)
    );

    foreach ($iterator as $file) {
        if ($file->isFile()
            && in_array($file->getExtension(), $extensions)
            && !str_contains($file->getPathname(), '/vendor/')
            && !str_contains($file->getPathname(), '/assets/')
            // PHPStan baseline is generated config, not code. See https://www.drupal.org/node/3426891
            && !str_ends_with($file->getPathname(), '.phpstan-baseline.php')) {
            $files[] = $file->getPathname();
        }
    }
    return $files;
}

/**
 * Parse *.services.yml files to extract service types (top-level prefix).
 * e.g., "entity_type.manager" → "entity_type"
 *       "cache.default" → "cache"
 *       "database" → "database" (no dot, use as-is)
 */
function collectServices(string $directory, SurfaceAreaCollector $surfaceArea): void
{
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($directory, RecursiveDirectoryIterator::SKIP_DOTS)
    );

    foreach ($iterator as $file) {
        if ($file->isFile()
            && str_ends_with($file->getFilename(), '.services.yml')
            && !str_contains($file->getPathname(), '/vendor/')
            && !str_contains($file->getPathname(), '/assets/')
            && !str_contains($file->getPathname(), '/tests/')
            && !str_contains($file->getPathname(), '/Tests/')) {

            $content = file_get_contents($file->getPathname());

            // Match service definitions: lines that start with 2 spaces followed by
            // a service ID (not starting with _ which are parameters/defaults)
            if (preg_match_all('/^  ([a-z][a-z0-9_.]+):\s*$/m', $content, $matches)) {
                foreach ($matches[1] as $serviceId) {
                    // Extract top-level type (before first dot, or full name if no dot)
                    $serviceType = explode('.', $serviceId)[0];
                    $surfaceArea->addService($serviceType);
                }
            }
        }
    }
}

/**
 * Extract hook pattern from function name.
 * e.g., "mymodule_form_alter" -> "hook_form_alter"
 *       "mymodule_preprocess_node" -> "hook_preprocess_THEME"
 */
function extractHookPattern(string $functionName, string $moduleName): ?string
{
    // Strip module name to get the hook name
    // e.g., "layout_builder_form_alter" with module "layout_builder" → "form_alter"
    if (!str_starts_with($functionName, $moduleName . '_')) {
        return null;
    }

    $hookName = substr($functionName, strlen($moduleName) + 1);

    // Normalize preprocess/process hooks (theme-specific)
    if (str_starts_with($hookName, 'preprocess_')) {
        return 'hook_preprocess_THEME';
    }
    if (str_starts_with($hookName, 'process_')) {
        return 'hook_process_THEME';
    }

    return 'hook_' . $hookName;
}

function extractModuleNameFromPath(string $filePath): ?string
{
    // Extract module name from .module file path
    // e.g., "core/modules/layout_builder/layout_builder.module" → "layout_builder"
    if (preg_match('/([a-z_]+)\.module$/', $filePath, $m)) {
        return $m[1];
    }
    return null;
}

/*
 * =============================================================================
 * AST VISITORS - ANTI-PATTERNS (contribute to per-file score)
 * =============================================================================
 */

/**
 * SERVICE LOCATOR - \Drupal:: static calls and $this->container->get() (anti-pattern)
 */
class ServiceLocatorVisitor extends NodeVisitorAbstract
{
    private AntipatternTracker $tracker;
    private string $currentFile = '';

    public function __construct(AntipatternTracker $tracker)
    {
        $this->tracker = $tracker;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentFile = $file;
    }

    public function enterNode(Node $node): ?int
    {
        // Detect \Drupal:: static calls
        if ($node instanceof Node\Expr\StaticCall
            && $node->class instanceof Node\Name) {
            $className = $node->class->toString();
            if ($className === 'Drupal' || $className === '\\Drupal') {
                $this->tracker->addServiceLocators($this->currentFile, SERVICE_LOCATOR_WEIGHT);
            }
            return null;
        }

        // Detect $this->container->get() calls
        if ($node instanceof Node\Expr\MethodCall
            && $node->name instanceof Node\Identifier
            && $node->name->name === 'get'
            && $node->var instanceof Node\Expr\PropertyFetch
            && $node->var->name instanceof Node\Identifier
            && $node->var->name->name === 'container') {
            $this->tracker->addServiceLocators($this->currentFile, SERVICE_LOCATOR_WEIGHT);
        }

        return null;
    }
}

/**
 * DEEP ARRAYS - Array access beyond 2 levels (anti-pattern)
 */
class DeepArrayVisitor extends NodeVisitorAbstract
{
    private AntipatternTracker $tracker;
    private string $currentFile = '';

    public function __construct(AntipatternTracker $tracker)
    {
        $this->tracker = $tracker;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentFile = $file;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Expr\ArrayDimFetch)) {
            return null;
        }

        $depth = 1;
        $current = $node->var;
        while ($current instanceof Node\Expr\ArrayDimFetch) {
            $depth++;
            $current = $current->var;
        }

        if ($depth > 2) {
            $this->tracker->addDeepArrays($this->currentFile, $depth - 2);
        }

        return NodeTraverser::DONT_TRAVERSE_CHILDREN;
    }
}

/**
 * DEEP ARRAYS - Array literals beyond 2 levels (anti-pattern)
 */
class DeepArrayLiteralVisitor extends NodeVisitorAbstract
{
    private AntipatternTracker $tracker;
    private string $currentFile = '';
    private int $currentDepth = 0;

    public function __construct(AntipatternTracker $tracker)
    {
        $this->tracker = $tracker;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentFile = $file;
        $this->currentDepth = 0;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Expr\Array_)) {
            return null;
        }

        $this->currentDepth++;
        if ($this->currentDepth > 2) {
            $this->tracker->addDeepArrays($this->currentFile, $this->currentDepth - 2);
        }
        return null;
    }

    public function leaveNode(Node $node): ?int
    {
        if ($node instanceof Node\Expr\Array_) {
            $this->currentDepth--;
        }
        return null;
    }
}

/*
 * =============================================================================
 * AST VISITORS - SURFACE AREA (collect distinct types)
 * =============================================================================
 */

/**
 * MAGIC KEYS - Collect distinct #-prefixed keys (surface area) and count occurrences (anti-pattern)
 *
 * Tracks two different metrics:
 * - Surface area: unique magic keys (vocabulary to learn), excluding common keys
 * - Anti-patterns: total magic key occurrences (pattern usage), including all keys
 */
class MagicKeyVisitor extends NodeVisitorAbstract
{
    private SurfaceAreaCollector $surfaceArea;
    private AntipatternTracker $antipatterns;
    private string $currentFile = '';

    public function __construct(SurfaceAreaCollector $surfaceArea, AntipatternTracker $antipatterns)
    {
        $this->surfaceArea = $surfaceArea;
        $this->antipatterns = $antipatterns;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentFile = $file;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Expr\ArrayItem)
            || !($node->key instanceof Node\Scalar\String_)
            || !str_starts_with($node->key->value, '#')) {
            return null;
        }

        $key = $node->key->value;

        // Skip color values like #000, #fff, #aabbcc
        if (preg_match('/^#[0-9a-fA-F]{3,6}$/', $key)) {
            return null;
        }

        // Skip keys that are just # or very short
        if (strlen($key) < 3) {
            return null;
        }

        // Count ALL magic key occurrences for anti-patterns (including common keys)
        $this->antipatterns->addMagicKeys($this->currentFile, 1);

        // Track unique non-common keys as surface area
        if (!in_array($key, IGNORED_KEYS)) {
            $this->surfaceArea->addMagicKey($key);
        }

        return null;
    }
}

/**
 * HOOKS - Collect distinct hooks from invocations (surface area)
 *
 * Instead of guessing hooks from function names, we detect actual hook
 * invocations in the codebase:
 * - $moduleHandler->invokeAll('hook_name', ...)
 * - $moduleHandler->alter('thing', ...) → hook_thing_alter
 * - module_invoke_all('hook_name', ...) (D7 style)
 *
 * This eliminates false positives from callbacks, form builders, etc.
 */
class HookTypeVisitor extends NodeVisitorAbstract
{
    private SurfaceAreaCollector $surfaceArea;
    private string $currentFile = '';

    public function __construct(SurfaceAreaCollector $surfaceArea)
    {
        $this->surfaceArea = $surfaceArea;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentFile = $file;
    }

    public function enterNode(Node $node): ?int
    {
        // Skip .api.php files (documentation examples, not real invocations)
        if (str_ends_with($this->currentFile, '.api.php')) {
            return null;
        }

        // Detect hook invocations via method calls
        if ($node instanceof Node\Expr\MethodCall) {
            $methodName = $node->name instanceof Node\Identifier ? $node->name->toString() : null;
            if ($methodName === null) {
                return null;
            }

            // ->invokeAll('hook_name', ...)
            if ($methodName === 'invokeAll' || $methodName === 'invoke') {
                $hookName = $this->extractFirstStringArg($node);
                if ($hookName) {
                    $this->surfaceArea->addHook('hook_' . $hookName);
                }
            }

            // ->alter('thing', ...) → hook_thing_alter
            if ($methodName === 'alter') {
                $alterName = $this->extractFirstStringArg($node);
                if ($alterName) {
                    $this->surfaceArea->addHook('hook_' . $alterName . '_alter');
                }
                // Also check for array of alter hooks: alter(['thing1', 'thing2'], ...)
                $this->extractAlterArrayArg($node);
            }
        }

        // Detect D7-style module_invoke_all('hook_name', ...)
        if ($node instanceof Node\Expr\FuncCall) {
            $funcName = $node->name instanceof Node\Name ? $node->name->toString() : null;
            if ($funcName === 'module_invoke_all' || $funcName === 'module_invoke') {
                $hookName = $this->extractFirstStringArg($node);
                if ($hookName) {
                    $this->surfaceArea->addHook('hook_' . $hookName);
                }
            }
            // drupal_alter('thing', ...) → hook_thing_alter
            if ($funcName === 'drupal_alter') {
                $alterName = $this->extractFirstStringArg($node);
                if ($alterName) {
                    $this->surfaceArea->addHook('hook_' . $alterName . '_alter');
                }
            }
            // module_implements('hook_name') - D7 style hook lookup
            if ($funcName === 'module_implements') {
                $hookName = $this->extractFirstStringArg($node);
                if ($hookName) {
                    $this->surfaceArea->addHook('hook_' . $hookName);
                }
            }
        }

        return null;
    }

    private function extractFirstStringArg(Node\Expr $node): ?string
    {
        $args = $node instanceof Node\Expr\MethodCall ? $node->args : ($node instanceof Node\Expr\FuncCall ? $node->args : []);
        if (empty($args)) {
            return null;
        }
        $firstArg = $args[0]->value;
        if ($firstArg instanceof Node\Scalar\String_) {
            return $firstArg->value;
        }
        return null;
    }

    private function extractAlterArrayArg(Node\Expr\MethodCall $node): void
    {
        if (empty($node->args)) {
            return;
        }
        $firstArg = $node->args[0]->value;
        if ($firstArg instanceof Node\Expr\Array_) {
            foreach ($firstArg->items as $item) {
                if ($item && $item->value instanceof Node\Scalar\String_) {
                    $this->surfaceArea->addHook('hook_' . $item->value->value . '_alter');
                }
            }
        }
    }
}

/**
 * PLUGIN TYPES - Collect distinct plugin managers (surface area)
 */
class PluginManagerVisitor extends NodeVisitorAbstract
{
    private SurfaceAreaCollector $surfaceArea;

    public function __construct(SurfaceAreaCollector $surfaceArea)
    {
        $this->surfaceArea = $surfaceArea;
    }

    public function setCurrentFile(string $file): void
    {
        // No per-file state needed
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Stmt\Class_) || $node->extends === null) {
            return null;
        }

        $parentClass = $node->extends->toString();
        if ($parentClass === 'DefaultPluginManager'
            || str_ends_with($parentClass, '\\DefaultPluginManager')) {
            $className = $node->name ? $node->name->toString() : 'anonymous';
            $this->surfaceArea->addPluginType($className);
        }
        return null;
    }
}

/**
 * EVENTS - Collect distinct Symfony events from EventSubscriberInterface (surface area)
 */
class EventSubscriberVisitor extends NodeVisitorAbstract
{
    private SurfaceAreaCollector $surfaceArea;
    private bool $inSubscriber = false;

    public function __construct(SurfaceAreaCollector $surfaceArea)
    {
        $this->surfaceArea = $surfaceArea;
    }

    public function setCurrentFile(string $file): void
    {
        $this->inSubscriber = false;
    }

    public function enterNode(Node $node): ?int
    {
        // Check if class implements EventSubscriberInterface
        if ($node instanceof Node\Stmt\Class_ && $node->implements) {
            foreach ($node->implements as $interface) {
                $name = $interface->toString();
                if ($name === 'EventSubscriberInterface'
                    || str_ends_with($name, '\\EventSubscriberInterface')) {
                    $this->inSubscriber = true;
                    break;
                }
            }
        }

        // Look for getSubscribedEvents method and extract event names
        if ($this->inSubscriber && $node instanceof Node\Stmt\ClassMethod
            && $node->name->toString() === 'getSubscribedEvents') {
            $this->extractEventsFromMethod($node);
        }

        return null;
    }

    public function leaveNode(Node $node): ?int
    {
        if ($node instanceof Node\Stmt\Class_) {
            $this->inSubscriber = false;
        }
        return null;
    }

    private function extractEventsFromMethod(Node\Stmt\ClassMethod $method): void
    {
        // Semantic approach: in getSubscribedEvents(), array keys ARE events.
        // Find anything used as an array key (ClassConstFetch or string literal).
        $this->findArrayKeys($method->stmts ?? []);
    }

    private function findArrayKeys(array $nodes): void
    {
        foreach ($nodes as $node) {
            if (!$node instanceof Node) {
                continue;
            }

            // Array dimension access: $events[EVENT_KEY] or $events[EVENT_KEY][]
            if ($node instanceof Node\Expr\ArrayDimFetch && $node->dim !== null) {
                $this->extractEventFromKey($node->dim);
            }

            // Array item in literal: [EVENT_KEY => ...]
            if ($node instanceof Node\Expr\ArrayItem && $node->key !== null) {
                $this->extractEventFromKey($node->key);
            }

            // Recurse into child nodes
            foreach ($node->getSubNodeNames() as $name) {
                $subNode = $node->$name;
                if (is_array($subNode)) {
                    $this->findArrayKeys($subNode);
                } elseif ($subNode instanceof Node) {
                    $this->findArrayKeys([$subNode]);
                }
            }
        }
    }

    private function extractEventFromKey(Node $key): void
    {
        // ClassConstFetch: KernelEvents::REQUEST
        if ($key instanceof Node\Expr\ClassConstFetch
            && $key->class instanceof Node\Name
            && $key->name instanceof Node\Identifier) {
            $constName = $key->name->toString();
            if ($constName !== 'class') {
                $className = $key->class->toString();
                $this->surfaceArea->addEvent($className . '::' . $constName);
            }
        }
        // String literal: 'kernel.request'
        elseif ($key instanceof Node\Scalar\String_) {
            $this->surfaceArea->addEvent($key->value);
        }
    }

}

/**
 * YAML FORMATS - Collect distinct YAML extension point formats
 *
 * Detects YAML formats via:
 * - new YamlDiscovery('format', ...) instantiations
 * - new YamlDiscoveryDecorator($discovery, 'format', ...)
 * - new YamlDirectoryDiscovery($dirs, 'format')
 * - String concatenations with .FORMAT.yml suffix
 * - getBasename('.FORMAT.yml') calls
 */
class YamlFormatVisitor extends NodeVisitorAbstract
{
    private SurfaceAreaCollector $surfaceArea;

    public function __construct(SurfaceAreaCollector $surfaceArea)
    {
        $this->surfaceArea = $surfaceArea;
    }

    public function setCurrentFile(string $file): void
    {
        // No per-file state needed
    }

    public function enterNode(Node $node): ?int
    {
        // Detect: new YamlDiscovery('format', ...)
        // Detect: new YamlDirectoryDiscovery($dirs, 'format')
        if ($node instanceof Node\Expr\New_
            && $node->class instanceof Node\Name) {
            $className = $node->class->toString();
            if (str_ends_with($className, 'YamlDiscovery')
                || str_ends_with($className, 'YamlDirectoryDiscovery')) {
                $format = $this->extractFirstStringArg($node->args);
                if ($format) {
                    $this->surfaceArea->addYamlFormat($format);
                }
            }
            // YamlDiscoveryDecorator has format as second argument
            if (str_ends_with($className, 'YamlDiscoveryDecorator')) {
                $format = $this->extractSecondStringArg($node->args);
                if ($format) {
                    $this->surfaceArea->addYamlFormat($format);
                }
            }
        }

        // Detect: $var . '.format.yml' or "/$module.format.yml"
        if ($node instanceof Node\Expr\BinaryOp\Concat) {
            $this->extractFromConcat($node);
        }

        // Detect: getBasename('.format.yml')
        if ($node instanceof Node\Expr\MethodCall
            && $node->name instanceof Node\Identifier
            && $node->name->name === 'getBasename') {
            $arg = $this->extractFirstStringArg($node->args);
            if ($arg && preg_match('/^\.([a-z_]+)\.yml$/', $arg, $matches)) {
                $this->surfaceArea->addYamlFormat($matches[1]);
            }
        }

        return null;
    }

    private function extractFirstStringArg(array $args): ?string
    {
        if (empty($args)) {
            return null;
        }
        $firstArg = $args[0]->value ?? null;
        if ($firstArg instanceof Node\Scalar\String_) {
            return $firstArg->value;
        }
        return null;
    }

    private function extractSecondStringArg(array $args): ?string
    {
        if (count($args) < 2) {
            return null;
        }
        $secondArg = $args[1]->value ?? null;
        if ($secondArg instanceof Node\Scalar\String_) {
            return $secondArg->value;
        }
        return null;
    }

    private function extractFromConcat(Node\Expr\BinaryOp\Concat $node): void
    {
        // Check right side for .format.yml pattern
        if ($node->right instanceof Node\Scalar\String_) {
            $value = $node->right->value;
            if (preg_match('/\.([a-z_]+)\.yml$/', $value, $matches)) {
                $this->surfaceArea->addYamlFormat($matches[1]);
            }
        }
        // Also check if the whole thing is a string with the pattern
        if ($node->left instanceof Node\Scalar\String_
            && $node->right instanceof Node\Scalar\String_) {
            $value = $node->left->value . $node->right->value;
            if (preg_match('/\.([a-z_]+)\.yml$/', $value, $matches)) {
                $this->surfaceArea->addYamlFormat($matches[1]);
            }
        }
    }
}

/**
 * INTERFACE METHODS - Collect distinct public methods on interfaces (surface area)
 *
 * Counts public methods on interfaces as these represent the API surface area
 * that implementations must satisfy. Each method is tracked as "InterfaceName::methodName".
 */
class InterfaceMethodVisitor extends NodeVisitorAbstract
{
    private SurfaceAreaCollector $surfaceArea;
    private ?string $currentInterface = null;

    public function __construct(SurfaceAreaCollector $surfaceArea)
    {
        $this->surfaceArea = $surfaceArea;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentInterface = null;
    }

    public function enterNode(Node $node): ?int
    {
        // Track when we enter an interface declaration
        if ($node instanceof Node\Stmt\Interface_) {
            $this->currentInterface = $node->name ? $node->name->toString() : null;
            return null;
        }

        // Count public methods within interfaces
        if ($this->currentInterface !== null && $node instanceof Node\Stmt\ClassMethod) {
            // Interface methods are implicitly public, but let's be explicit
            if ($node->isPublic() || !$node->isPrivate() && !$node->isProtected()) {
                $methodName = $node->name->toString();
                $this->surfaceArea->addInterfaceMethod($this->currentInterface . '::' . $methodName);
            }
        }

        return null;
    }

    public function leaveNode(Node $node): ?int
    {
        if ($node instanceof Node\Stmt\Interface_) {
            $this->currentInterface = null;
        }
        return null;
    }
}

/**
 * CYCLOMATIC COMPLEXITY
 */
class CcnVisitor extends NodeVisitorAbstract
{
    private FileMetricsTracker $metrics;
    private string $currentFile = '';

    public function __construct(FileMetricsTracker $metrics)
    {
        $this->metrics = $metrics;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentFile = $file;
    }

    public function enterNode(Node $node): ?int
    {
        $points = 0;

        if ($node instanceof Node\Stmt\If_
            || $node instanceof Node\Stmt\ElseIf_
            || $node instanceof Node\Stmt\While_
            || $node instanceof Node\Stmt\For_
            || $node instanceof Node\Stmt\Foreach_
            || $node instanceof Node\Stmt\Case_
            || $node instanceof Node\Stmt\Catch_
            || $node instanceof Node\Stmt\Do_) {
            $points = 1;
        }
        elseif ($node instanceof Node\Expr\BinaryOp\BooleanAnd
            || $node instanceof Node\Expr\BinaryOp\BooleanOr
            || $node instanceof Node\Expr\BinaryOp\LogicalAnd
            || $node instanceof Node\Expr\BinaryOp\LogicalOr) {
            $points = 1;
        }
        elseif ($node instanceof Node\Expr\Ternary
            || $node instanceof Node\Expr\BinaryOp\Coalesce) {
            $points = 1;
        }

        if ($points > 0) {
            $this->metrics->addCcn($this->currentFile, $points);
        }
        return null;
    }
}

/*
 * =============================================================================
 * MAIN EXECUTION
 * =============================================================================
 */

if ($argc < 2) {
    fwrite(STDERR, "Usage: php drupalisms.php /path/to/drupal/core\n");
    exit(1);
}

$coreDirectory = $argv[1];
if (!is_dir($coreDirectory)) {
    fwrite(STDERR, "Error: Directory not found: $coreDirectory\n");
    exit(1);
}

// Set up parser and trackers
$parser = (new ParserFactory())->createForNewestSupportedVersion();
$fileMetrics = new FileMetricsTracker();
$antipatterns = new AntipatternTracker($fileMetrics);
$surfaceArea = new SurfaceAreaCollector();

// Hardcoded YAML formats that use directory-based discovery (not detectable via AST)
$surfaceArea->addYamlFormat('schema');

// CCN visitor (runs on ALL files for file metrics)
$ccnVisitor = new CcnVisitor($fileMetrics);
$metricsTraverser = new NodeTraverser();
$metricsTraverser->addVisitor($ccnVisitor);

// Anti-pattern visitors (runs only on production files)
$serviceLocatorVisitor = new ServiceLocatorVisitor($antipatterns);
$deepArrayVisitor = new DeepArrayVisitor($antipatterns);
$deepArrayLiteralVisitor = new DeepArrayLiteralVisitor($antipatterns);

// Surface area visitors (runs only on production files)
$magicKeyVisitor = new MagicKeyVisitor($surfaceArea, $antipatterns);
$hookTypeVisitor = new HookTypeVisitor($surfaceArea);
$pluginManagerVisitor = new PluginManagerVisitor($surfaceArea);
$eventSubscriberVisitor = new EventSubscriberVisitor($surfaceArea);
$yamlFormatVisitor = new YamlFormatVisitor($surfaceArea);
$interfaceMethodVisitor = new InterfaceMethodVisitor($surfaceArea);

$productionTraverser = new NodeTraverser();
$productionTraverser->addVisitor($serviceLocatorVisitor);
$productionTraverser->addVisitor($deepArrayVisitor);
$productionTraverser->addVisitor($deepArrayLiteralVisitor);
$productionTraverser->addVisitor($magicKeyVisitor);
$productionTraverser->addVisitor($hookTypeVisitor);
$productionTraverser->addVisitor($pluginManagerVisitor);
$productionTraverser->addVisitor($eventSubscriberVisitor);
$productionTraverser->addVisitor($yamlFormatVisitor);
$productionTraverser->addVisitor($interfaceMethodVisitor);

// Process all files
$files = findPhpFiles($coreDirectory);
$parseErrors = 0;

foreach ($files as $filePath) {
    try {
        $code = file_get_contents($filePath);
        $relativePath = str_replace($coreDirectory . '/', '', $filePath);
        $isTest = isTestFile($relativePath);
        $loc = countLinesOfCode($code);

        $fileMetrics->initFile($relativePath, $isTest, $loc);

        $ast = $parser->parse($code);
        if ($ast !== null) {
            // CCN runs on all files (needed for file metrics)
            $ccnVisitor->setCurrentFile($relativePath);
            $metricsTraverser->traverse($ast);

            // Surface area and anti-patterns only run on production code
            if (!$isTest) {
                $serviceLocatorVisitor->setCurrentFile($relativePath);
                $deepArrayVisitor->setCurrentFile($relativePath);
                $deepArrayLiteralVisitor->setCurrentFile($relativePath);
                $magicKeyVisitor->setCurrentFile($relativePath);
                $hookTypeVisitor->setCurrentFile($relativePath);
                $pluginManagerVisitor->setCurrentFile($relativePath);
                $eventSubscriberVisitor->setCurrentFile($relativePath);
                $yamlFormatVisitor->setCurrentFile($relativePath);
                $interfaceMethodVisitor->setCurrentFile($relativePath);
                $productionTraverser->traverse($ast);
            }
        }

        $fileMetrics->calculateMi($relativePath);
    } catch (Exception $e) {
        $parseErrors++;
    }
}

// Collect service types from *.services.yml files
collectServices($coreDirectory, $surfaceArea);

// Calculate aggregates
$allFiles = $fileMetrics->getFiles();
$productionFiles = array_filter($allFiles, fn($f) => !$f['test']);
$testFiles = array_filter($allFiles, fn($f) => $f['test']);

function calculateAggregates(array $files): array
{
    if (empty($files)) {
        return ['loc' => 0, 'ccn' => 0, 'mi' => 0, 'antipatterns' => 0];
    }

    $totalLoc = array_sum(array_column($files, 'loc'));
    $totalCcn = array_sum(array_column($files, 'ccn'));
    $totalAntipatterns = array_sum(array_column($files, 'antipatterns'));

    $weightedMi = 0;
    foreach ($files as $f) {
        $weightedMi += $f['mi'] * $f['loc'];
    }
    $avgMi = $totalLoc > 0 ? $weightedMi / $totalLoc : 0;
    $avgCcn = count($files) > 0 ? $totalCcn / count($files) : 0;
    $antipatternsDensity = $totalLoc > 0 ? ($totalAntipatterns / $totalLoc) * 1000 : 0;

    return [
        'loc' => $totalLoc,
        'ccn' => round($avgCcn, 1),
        'mi' => round($avgMi, 1),
        'antipatterns' => round($antipatternsDensity, 1),
    ];
}

$productionAggregates = calculateAggregates($productionFiles);
$testAggregates = calculateAggregates($testFiles);

// Output JSON
$output = [
    'production' => $productionAggregates,
    'test' => $testAggregates,
    'surfaceArea' => $surfaceArea->getCounts(),
    'surfaceAreaLists' => $surfaceArea->getLists(),
    'antipatterns' => $antipatterns->getCounts(),
    'files' => $allFiles,
    'filesAnalyzed' => count($files),
    'parseErrors' => $parseErrors,
];

echo json_encode($output, JSON_PRETTY_PRINT) . "\n";
