#!/usr/bin/env php
<?php
ini_set('memory_limit', '512M');

/**
 * @file
 * Analyze Drupal codebase for anti-patterns and API surface area.
 *
 * Two separate metrics:
 *
 * 1. ANTI-PATTERNS (per-function, density metric - should decrease)
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
 * Output: JSON with anti-patterns, surface area, and function-level metrics
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

/**
 * Hooks that can't be reliably detected via AST analysis.
 *
 * These hooks are well-documented but our analysis misses them:
 * - ModuleInstaller::invoke() for install/uninstall/schema
 * - UpdateRegistry for update_N and post_update_NAME
 * - Install system for install_tasks
 * - Theme registry for preprocess/process hooks
 * - Variable indirection: $hooks = ['form']; $this->alter($hooks, ...)
 */
const IMPLICIT_HOOKS = [
    'hook_install',
    'hook_uninstall',
    'hook_schema',
    'hook_update_N',
    'hook_post_update_NAME',
    'hook_requirements',
    'hook_install_tasks',
    'hook_install_tasks_alter',
    'hook_preprocess_HOOK',
    'hook_process_HOOK',
    'hook_form_alter',
    'hook_hook_info',
    // Entity lifecycle hooks (invoked via string concatenation)
    'hook_entity_insert',
    'hook_entity_update',
    'hook_entity_delete',
    'hook_entity_presave',
    'hook_entity_predelete',
    'hook_entity_create',
    'hook_entity_load',
    'hook_entity_view',
    'hook_entity_access',
    // Block/query/plugin filter generics (specific instances filtered)
    'hook_block_build_alter',
    'hook_block_view_alter',
    'hook_query_alter',
    'hook_jsonapi_entity_filter_access',
    'hook_entity_view_alter',
];

/**
 * Patterns for specific hook instances (matching hook names without 'hook_' prefix).
 *
 * These patterns identify hooks that are specific implementations of generic patterns.
 * For example, hook_node_insert matches this pattern and is filtered because it's
 * covered by the generic hook_entity_insert in IMPLICIT_HOOKS.
 *
 * Applied globally to ALL detected hooks (from ModuleHandler, #[Hook], and implicit).
 */
const SPECIFIC_HOOK_PATTERNS = [
    '/^(preprocess|process)_.+$/',           // preprocess_HOOK, process_HOOK
    '/^form_.+_alter$/',                     // form_FORM_ID_alter, form_BASE_FORM_ID_alter
    '/^theme_suggestions_.+$/',              // theme_suggestions_HOOK, theme_suggestions_HOOK_alter
    '/^update_\d+$/',                        // update_N
    '/^post_update_.+$/',                    // post_update_NAME
    // Entity type lifecycle hooks (ENTITY_TYPE_OPERATION) - excludes generics and known false positives
    '/^(?!entity_|field_attach_|field_storage_pre_|field_access$|file_download_|jsonapi_|views_pre_|mass_|translate_|image_style_)[a-z_]+_(insert|update|delete|presave|predelete|create|load|view|access)$/',
    // Entity types starting with 'entity_' (entity_form_mode, entity_view_mode, entity_view_display)
    '/^entity_(form_mode|view_mode|view_display)_(insert|update|delete|presave|predelete|create|load|view|access)$/',
    // Block build/view specific alters (block_build_BLOCK_ID_alter, block_view_BLOCK_ID_alter)
    '/^block_build_(?!alter$).+_alter$/',
    '/^block_view_(?!alter$).+_alter$/',
    // Query tag specific alters (query_TAG_alter)
    '/^query_(?!alter$).+_alter$/',
    // Plugin filter with consumer (plugin_filter_TYPE__CONSUMER_alter - double underscore)
    '/^plugin_filter_[a-z_]+__[a-z_]+_alter$/',
    // JSON API entity-type-specific filter access (jsonapi_ENTITY_TYPE_filter_access)
    '/^jsonapi_(?!entity_filter_access$)[a-z_]+_filter_access$/',
    // Entity-type-specific view_alter (ENTITY_TYPE_view_alter, but not entity_view_alter or block_view_alter etc.)
    '/^(node|user|comment|taxonomy_term|media|file)_view_alter$/',
];

/**
 * Patterns for identifying hook implementations in function names.
 *
 * Used to filter hook implementations from global functions.
 * {module} is replaced with the actual module name at runtime.
 * Patterns without {module} match regardless of module context.
 */
const HOOK_IMPLEMENTATION_PATTERNS = [
    '/^[a-z0-9_]+_update_\d+$/',                                  // hook_update_N
    '/^[a-z0-9_]+_post_update_[a-z0-9_]+$/',                      // hook_post_update_NAME
    '/^[a-z0-9_]+_(update_last_removed|update_dependencies|removed_post_updates)$/',
    '/^template_(preprocess|process)_.+$/',                       // template_preprocess_HOOK
    '/^{module}_(preprocess|process)_.+$/',                       // hook_preprocess_HOOK
    '/^{module}_form_.+_alter$/',                                 // hook_form_FORM_ID_alter
    '/^{module}_theme_suggestions_.+_alter$/',                    // hook_theme_suggestions_HOOK_alter
];

/*
 * =============================================================================
 * FUNCTION METRICS TRACKER
 * =============================================================================
 */

/**
 * Tracks metrics per function/method (production code only).
 *
 * Each function has: name, file, loc, ccn, mi, antipatterns
 */
class FunctionMetricsTracker
{
    private array $functions = [];
    private ?string $currentFunction = null;
    private ?string $currentFile = null;

    public function enterFunction(string $name, string $file): void
    {
        $this->currentFunction = $name;
        $this->currentFile = $file;
        $key = $file . '::' . $name;
        $this->functions[$key] = [
            'name' => $name,
            'file' => $file,
            'loc' => 0,
            'ccn' => 1,
            'mi' => 100,
            'antipatterns' => 0,
        ];
    }

    public function leaveFunction(int $loc): void
    {
        if ($this->currentFunction === null) {
            return;
        }
        $key = $this->currentFile . '::' . $this->currentFunction;
        if (isset($this->functions[$key])) {
            $this->functions[$key]['loc'] = $loc;
            $this->calculateMi($key);
        }
        $this->currentFunction = null;
    }

    public function addCcn(int $points): void
    {
        if ($this->currentFunction === null) {
            return;
        }
        $key = $this->currentFile . '::' . $this->currentFunction;
        if (isset($this->functions[$key])) {
            $this->functions[$key]['ccn'] += $points;
        }
    }

    public function addAntipatterns(int $score): void
    {
        if ($this->currentFunction === null) {
            return;
        }
        $key = $this->currentFile . '::' . $this->currentFunction;
        if (isset($this->functions[$key])) {
            $this->functions[$key]['antipatterns'] += $score;
        }
    }

    public function isInFunction(): bool
    {
        return $this->currentFunction !== null;
    }

    private function calculateMi(string $key): void
    {
        $f = &$this->functions[$key];
        $loc = max($f['loc'], 1);
        $ccn = max($f['ccn'], 1);

        $volume = $loc * 5;
        $mi = 171 - 5.2 * log($volume) - 0.23 * $ccn - 16.2 * log($loc);
        $f['mi'] = (int) max(0, min(100, $mi));
    }

    public function getFunctions(): array
    {
        return array_values($this->functions);
    }
}

/**
 * Tracks anti-pattern occurrence counts by category (codebase level).
 */
class AntipatternTracker
{
    private FunctionMetricsTracker $metrics;
    private int $serviceLocators = 0;
    private int $deepArrays = 0;
    private int $magicKeys = 0;

    public function __construct(FunctionMetricsTracker $metrics)
    {
        $this->metrics = $metrics;
    }

    public function addServiceLocators(int $score): void
    {
        $this->serviceLocators += $score;
        $this->metrics->addAntipatterns($score);
    }

    public function addDeepArrays(int $score): void
    {
        $this->deepArrays += $score;
        $this->metrics->addAntipatterns($score);
    }

    public function addMagicKeys(int $score): void
    {
        $this->magicKeys += $score;
        $this->metrics->addAntipatterns($score);
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
    public array $globalFunctions = [];

    // All public procedural functions: function name → module name
    private array $functions = [];

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

    public function addFunction(string $name, string $moduleName): void
    {
        $this->functions[$name] = $moduleName;
    }

    /**
     * Add implicit hooks that can't be detected via AST analysis.
     */
    public function addImplicitHooks(): void
    {
        foreach (IMPLICIT_HOOKS as $hook) {
            $this->hooks[$hook] = true;
        }
    }

    /**
     * Filter candidate functions to remove hook implementations.
     */
    public function filterHookImplementations(): void
    {
        // Build hook patterns (hook_form_alter → form_alter)
        $hookPatterns = [];
        foreach (array_keys($this->hooks) as $hook) {
            if (str_starts_with($hook, 'hook_')) {
                $hookPatterns[] = substr($hook, 5);
            }
        }

        // Keep functions that are not hook implementations
        foreach ($this->functions as $functionName => $moduleName) {
            if ($this->isHookImplementation($functionName, $moduleName, $hookPatterns)) {
                continue;
            }
            $this->globalFunctions[$functionName] = true;
        }
    }

    /**
     * Check if a function is a hook implementation.
     */
    private function isHookImplementation(string $functionName, string $moduleName, array $hookPatterns): bool
    {
        // Exact match against detected hooks (hook_help → mymodule_help)
        if ($moduleName !== '') {
            foreach ($hookPatterns as $suffix) {
                if ($functionName === $moduleName . '_' . $suffix) {
                    return true;
                }
            }
        }

        // Pattern match against dynamic hooks (hook_update_N → mymodule_update_8001)
        foreach (HOOK_IMPLEMENTATION_PATTERNS as $pattern) {
            if (str_contains($pattern, '{module}')) {
                if ($moduleName === '') {
                    continue;
                }
                $pattern = str_replace('{module}', preg_quote($moduleName, '/'), $pattern);
            }
            if (preg_match($pattern, $functionName)) {
                return true;
            }
        }

        return false;
    }

    /**
     * Filter out specific hook instances covered by generic patterns.
     *
     * Removes hooks like hook_node_insert (covered by hook_entity_insert) and
     * hook_preprocess_block (covered by hook_preprocess_HOOK) to avoid counting
     * both the generic pattern and its specific implementations.
     *
     * Must be called after all hooks are collected (ModuleHandler, #[Hook], implicit).
     */
    public function filterSpecificHookInstances(): void
    {
        $genericHooks = [];
        foreach (array_keys($this->hooks) as $hook) {
            $hookName = str_starts_with($hook, 'hook_') ? substr($hook, 5) : $hook;
            $matchesSpecificPattern = false;
            foreach (SPECIFIC_HOOK_PATTERNS as $pattern) {
                if (preg_match($pattern, $hookName)) {
                    $matchesSpecificPattern = true;
                    break;
                }
            }
            if (!$matchesSpecificPattern) {
                $genericHooks[$hook] = true;
            }
        }
        $this->hooks = $genericHooks;
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
            'globalFunctions' => count($this->globalFunctions),
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
            'globalFunctions' => array_keys($this->globalFunctions),
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
        $path = $file->getPathname();
        if ($file->isFile()
            && in_array($file->getExtension(), $extensions)
            // /assets: compiled CSS/JS, not PHP source
            && !str_contains($path, '/assets/')
            // /vendor: third-party dependencies, not Drupal core code
            && !str_contains($path, '/vendor/')
            // .api.php: documentation stubs showing hook signatures, not real implementations
            && !str_ends_with($path, '.api.php')
            // .phpstan-baseline.php: auto-generated config, not code (see https://www.drupal.org/node/3426891)
            && !str_ends_with($path, '.phpstan-baseline.php')) {
            $files[] = $path;
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

/*
 * =============================================================================
 * AST VISITORS - ANTI-PATTERNS (contribute to per-function score)
 * =============================================================================
 */

/**
 * SERVICE LOCATOR - \Drupal:: static calls and $this->container->get() (anti-pattern)
 */
class ServiceLocatorVisitor extends NodeVisitorAbstract
{
    private AntipatternTracker $tracker;

    public function __construct(AntipatternTracker $tracker)
    {
        $this->tracker = $tracker;
    }

    public function enterNode(Node $node): ?int
    {
        // Detect \Drupal:: static calls
        if ($node instanceof Node\Expr\StaticCall
            && $node->class instanceof Node\Name) {
            $className = $node->class->toString();
            if ($className === 'Drupal' || $className === '\\Drupal') {
                $this->tracker->addServiceLocators(SERVICE_LOCATOR_WEIGHT);
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
            $this->tracker->addServiceLocators(SERVICE_LOCATOR_WEIGHT);
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

    public function __construct(AntipatternTracker $tracker)
    {
        $this->tracker = $tracker;
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
            $this->tracker->addDeepArrays($depth - 2);
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
    private int $currentDepth = 0;

    public function __construct(AntipatternTracker $tracker)
    {
        $this->tracker = $tracker;
    }

    public function resetDepth(): void
    {
        $this->currentDepth = 0;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Expr\Array_)) {
            return null;
        }

        $this->currentDepth++;
        if ($this->currentDepth > 2) {
            $this->tracker->addDeepArrays($this->currentDepth - 2);
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

    public function __construct(SurfaceAreaCollector $surfaceArea, AntipatternTracker $antipatterns)
    {
        $this->surfaceArea = $surfaceArea;
        $this->antipatterns = $antipatterns;
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
        $this->antipatterns->addMagicKeys(1);

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
 * Detects hook invocations via ModuleHandler methods and legacy D7 functions.
 * Also includes IMPLICIT_HOOKS that are invoked through special mechanisms.
 */
class HookTypeVisitor extends NodeVisitorAbstract
{
    private SurfaceAreaCollector $surfaceArea;
    private string $currentFile = '';

    // ModuleHandler invoke methods: method name → argument index of hook name
    private const INVOKE_METHODS = [
        'invoke' => 1,                // invoke($module, $hook) - hook is second arg
        'invokeAll' => 0,
        'invokeAllWith' => 0,
        'hasImplementations' => 0,
        'invokeDeprecated' => 2,      // invokeDeprecated($message, $module, $hook)
        'invokeAllDeprecated' => 1,   // invokeAllDeprecated($message, $hook)
    ];

    // ModuleHandler alter methods: method name → argument index of alter type
    private const ALTER_METHODS = [
        'alter' => 0,
        'alterDeprecated' => 1,       // first arg is deprecation message
    ];

    // Legacy D7 functions (all use first argument)
    private const LEGACY_INVOKE_FUNCTIONS = ['module_invoke', 'module_invoke_all', 'module_implements'];
    private const LEGACY_ALTER_FUNCTIONS = ['drupal_alter'];

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
        // Detect hook declarations via #[Hook('name')] attributes (Drupal 11.1+)
        if ($node instanceof Node\Stmt\Class_ || $node instanceof Node\Stmt\ClassMethod) {
            foreach ($node->attrGroups as $attrGroup) {
                foreach ($attrGroup->attrs as $attr) {
                    $attrName = $attr->name->toString();
                    // Match Hook or fully qualified Drupal\Core\Hook\Attribute\Hook
                    if ($attrName === 'Hook' || str_ends_with($attrName, '\Hook')) {
                        if (!empty($attr->args) && $attr->args[0]->value instanceof Node\Scalar\String_) {
                            $hookName = $attr->args[0]->value->value;
                            $this->surfaceArea->addHook('hook_' . $hookName);
                        }
                    }
                }
            }
        }

        // Detect hook invocations via method calls
        if ($node instanceof Node\Expr\MethodCall) {
            $methodName = $node->name instanceof Node\Identifier ? $node->name->toString() : null;
            if ($methodName === null) {
                return null;
            }

            if (isset(self::INVOKE_METHODS[$methodName])) {
                $index = self::INVOKE_METHODS[$methodName];
                $hookName = $this->extractStringArg($node, $index);
                if ($hookName) {
                    $this->surfaceArea->addHook('hook_' . $hookName);
                }
            }

            if (isset(self::ALTER_METHODS[$methodName])) {
                $index = self::ALTER_METHODS[$methodName];
                $alterName = $this->extractStringArg($node, $index);
                if ($alterName) {
                    $this->surfaceArea->addHook('hook_' . $alterName . '_alter');
                }
                $this->extractAlterArrayArg($node, $index);
            }
        }

        // Detect legacy D7-style function calls
        if ($node instanceof Node\Expr\FuncCall) {
            $funcName = $node->name instanceof Node\Name ? $node->name->toString() : null;

            if (in_array($funcName, self::LEGACY_INVOKE_FUNCTIONS, true)) {
                $hookName = $this->extractStringArg($node, 0);
                if ($hookName) {
                    $this->surfaceArea->addHook('hook_' . $hookName);
                }
            }

            if (in_array($funcName, self::LEGACY_ALTER_FUNCTIONS, true)) {
                $alterName = $this->extractStringArg($node, 0);
                if ($alterName) {
                    $this->surfaceArea->addHook('hook_' . $alterName . '_alter');
                }
            }
        }

        return null;
    }

    private function extractStringArg(Node\Expr $node, int $index): ?string
    {
        $args = $node instanceof Node\Expr\MethodCall ? $node->args : ($node instanceof Node\Expr\FuncCall ? $node->args : []);
        if (count($args) <= $index) {
            return null;
        }
        $arg = $args[$index]->value;
        if ($arg instanceof Node\Scalar\String_) {
            return $arg->value;
        }
        return null;
    }

    private function extractAlterArrayArg(Node\Expr\MethodCall $node, int $index): void
    {
        if (count($node->args) <= $index) {
            return;
        }
        $arg = $node->args[$index]->value;
        if ($arg instanceof Node\Expr\Array_) {
            foreach ($arg->items as $item) {
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
 * GLOBAL FUNCTIONS - Collect distinct procedural functions (surface area)
 *
 * Global functions are: all procedural functions not starting with _ that are
 * not hook implementations (since hooks are already counted separately).
 *
 * Functions are collected during traversal, then filtered post-traversal
 * to remove hook implementations once all hooks are known.
 */
class GlobalFunctionVisitor extends NodeVisitorAbstract
{
    private SurfaceAreaCollector $surfaceArea;
    private string $currentModuleName = '';

    public function __construct(SurfaceAreaCollector $surfaceArea)
    {
        $this->surfaceArea = $surfaceArea;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentModuleName = $this->extractModuleName($file);
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Stmt\Function_)) {
            return null;
        }

        $functionName = $node->name->toString();

        // Skip internal functions (prefixed with _)
        if (str_starts_with($functionName, '_')) {
            return null;
        }

        // Store for later filtering (hook implementations removed post-traversal)
        $this->surfaceArea->addFunction($functionName, $this->currentModuleName);

        return null;
    }

    /**
     * Extract module/theme name from file path.
     */
    private function extractModuleName(string $file): string
    {
        // node.module → node, olivero.theme → olivero
        if (preg_match('/([^\/]+)\.(module|install|theme)$/', $file, $matches)) {
            return $matches[1];
        }
        // themes/olivero/theme-settings.php → olivero
        if (preg_match('/\/([^\/]+)\/theme-settings\.php$/', $file, $matches)) {
            return $matches[1];
        }
        return '';
    }
}

/**
 * FUNCTION/METHOD TRACKER - Tracks entry/exit of functions and methods
 *
 * This visitor must be added FIRST to the traverser so it sets up function context
 * before other visitors (CCN, antipatterns) add their metrics.
 */
class FunctionBoundaryVisitor extends NodeVisitorAbstract
{
    private FunctionMetricsTracker $metrics;
    private string $currentFile = '';
    private ?string $currentClassName = null;
    private ?int $functionStartLine = null;
    private ?int $functionEndLine = null;
    private string $code = '';

    public function __construct(FunctionMetricsTracker $metrics)
    {
        $this->metrics = $metrics;
    }

    public function setContext(string $file, string $code): void
    {
        $this->currentFile = $file;
        $this->currentClassName = null;
        $this->code = $code;
    }

    public function enterNode(Node $node): ?int
    {
        // Track class name for method naming
        if ($node instanceof Node\Stmt\Class_ || $node instanceof Node\Stmt\Trait_) {
            $this->currentClassName = $node->name ? $node->name->toString() : 'anonymous';
        }

        // Enter a function or method
        if ($node instanceof Node\Stmt\Function_) {
            $name = $node->name->toString();
            $this->functionStartLine = $node->getStartLine();
            $this->functionEndLine = $node->getEndLine();
            $this->metrics->enterFunction($name, $this->currentFile);
        } elseif ($node instanceof Node\Stmt\ClassMethod) {
            $className = $this->currentClassName ?? 'Unknown';
            $name = $className . '::' . $node->name->toString();
            $this->functionStartLine = $node->getStartLine();
            $this->functionEndLine = $node->getEndLine();
            $this->metrics->enterFunction($name, $this->currentFile);
        }

        return null;
    }

    public function leaveNode(Node $node): ?int
    {
        // Leave class
        if ($node instanceof Node\Stmt\Class_ || $node instanceof Node\Stmt\Trait_) {
            $this->currentClassName = null;
        }

        // Leave a function or method
        if ($node instanceof Node\Stmt\Function_ || $node instanceof Node\Stmt\ClassMethod) {
            $loc = $this->countFunctionLoc();
            $this->metrics->leaveFunction($loc);
            $this->functionStartLine = null;
            $this->functionEndLine = null;
        }

        return null;
    }

    private function countFunctionLoc(): int
    {
        if ($this->functionStartLine === null || $this->functionEndLine === null) {
            return 0;
        }

        $lines = explode("\n", $this->code);
        $count = 0;
        $inBlockComment = false;

        for ($i = $this->functionStartLine - 1; $i < $this->functionEndLine && $i < count($lines); $i++) {
            $trimmed = trim($lines[$i]);
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
}

/**
 * CYCLOMATIC COMPLEXITY - Tracks CCN per function/method
 */
class CcnVisitor extends NodeVisitorAbstract
{
    private FunctionMetricsTracker $metrics;

    public function __construct(FunctionMetricsTracker $metrics)
    {
        $this->metrics = $metrics;
    }

    public function enterNode(Node $node): ?int
    {
        // Only count if we're inside a function
        if (!$this->metrics->isInFunction()) {
            return null;
        }

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
            $this->metrics->addCcn($points);
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
$functionMetrics = new FunctionMetricsTracker();
$antipatterns = new AntipatternTracker($functionMetrics);
$surfaceArea = new SurfaceAreaCollector();

// Hardcoded YAML formats that use directory-based discovery (not detectable via AST)
$surfaceArea->addYamlFormat('schema');

// Function boundary visitor must be first to set up function context
$functionBoundaryVisitor = new FunctionBoundaryVisitor($functionMetrics);
$ccnVisitor = new CcnVisitor($functionMetrics);

// Anti-pattern visitors
$serviceLocatorVisitor = new ServiceLocatorVisitor($antipatterns);
$deepArrayVisitor = new DeepArrayVisitor($antipatterns);
$deepArrayLiteralVisitor = new DeepArrayLiteralVisitor($antipatterns);

// Surface area visitors
$magicKeyVisitor = new MagicKeyVisitor($surfaceArea, $antipatterns);
$hookTypeVisitor = new HookTypeVisitor($surfaceArea);
$pluginManagerVisitor = new PluginManagerVisitor($surfaceArea);
$eventSubscriberVisitor = new EventSubscriberVisitor($surfaceArea);
$yamlFormatVisitor = new YamlFormatVisitor($surfaceArea);
$interfaceMethodVisitor = new InterfaceMethodVisitor($surfaceArea);
$globalFunctionVisitor = new GlobalFunctionVisitor($surfaceArea);

// Single traverser with function boundary visitor first
$traverser = new NodeTraverser();
$traverser->addVisitor($functionBoundaryVisitor);
$traverser->addVisitor($ccnVisitor);
$traverser->addVisitor($serviceLocatorVisitor);
$traverser->addVisitor($deepArrayVisitor);
$traverser->addVisitor($deepArrayLiteralVisitor);
$traverser->addVisitor($magicKeyVisitor);
$traverser->addVisitor($hookTypeVisitor);
$traverser->addVisitor($pluginManagerVisitor);
$traverser->addVisitor($eventSubscriberVisitor);
$traverser->addVisitor($yamlFormatVisitor);
$traverser->addVisitor($interfaceMethodVisitor);
$traverser->addVisitor($globalFunctionVisitor);

// Track total LOC per file for codebase totals
$productionLoc = 0;
$testLoc = 0;

// Process all files
$files = findPhpFiles($coreDirectory);
$parseErrors = 0;

foreach ($files as $filePath) {
    try {
        $code = file_get_contents($filePath);
        $relativePath = str_replace($coreDirectory . '/', '', $filePath);
        $isTest = isTestFile($relativePath);
        $loc = countLinesOfCode($code);

        if ($isTest) {
            // Test files: only count LOC, skip all other analysis
            $testLoc += $loc;
            continue;
        }

        $productionLoc += $loc;

        $ast = $parser->parse($code);
        if ($ast !== null) {
            $functionBoundaryVisitor->setContext($relativePath, $code);
            $deepArrayLiteralVisitor->resetDepth();
            $hookTypeVisitor->setCurrentFile($relativePath);
            $eventSubscriberVisitor->setCurrentFile($relativePath);
            $interfaceMethodVisitor->setCurrentFile($relativePath);
            $globalFunctionVisitor->setCurrentFile($relativePath);
            $traverser->traverse($ast);
        }
    } catch (Exception $e) {
        $parseErrors++;
    }
}

// Collect service types from *.services.yml files
collectServices($coreDirectory, $surfaceArea);

// Get function data (production only - tests were skipped earlier)
$functions = $functionMetrics->getFunctions();

/**
 * Calculate aggregates from function data.
 */
function calculateAggregates(array $functions, int $totalLoc): array
{
    if (empty($functions)) {
        return [
            'loc' => $totalLoc,
            'functions' => 0,
            'ccn' => ['avg' => 0, 'p95' => 0],
            'mi' => ['avg' => 0, 'p5' => 0],
            'antipatterns' => 0,
        ];
    }

    $count = count($functions);
    $ccnValues = array_column($functions, 'ccn');
    $miValues = array_column($functions, 'mi');
    $locValues = array_column($functions, 'loc');
    $antipatternValues = array_column($functions, 'antipatterns');

    // CCN average
    $ccnAvg = array_sum($ccnValues) / $count;

    // CCN 95th percentile (higher = worse)
    sort($ccnValues);
    $p95Index = (int) ceil(0.95 * $count) - 1;
    $ccnP95 = $ccnValues[max(0, min($p95Index, $count - 1))];

    // MI weighted average (by LOC)
    $totalFuncLoc = array_sum($locValues);
    $weightedMi = 0;
    foreach ($functions as $f) {
        $weightedMi += $f['mi'] * $f['loc'];
    }
    $avgMi = $totalFuncLoc > 0 ? $weightedMi / $totalFuncLoc : 0;

    // MI 5th percentile (lower = worse, so P5 shows the worst functions)
    sort($miValues);
    $p5Index = (int) floor(0.05 * $count);
    $miP5 = $miValues[max(0, min($p5Index, $count - 1))];

    // Antipatterns density (per 1000 LOC)
    $totalAntipatterns = array_sum($antipatternValues);
    $antipatternsDensity = $totalLoc > 0 ? ($totalAntipatterns / $totalLoc) * 1000 : 0;

    return [
        'loc' => $totalLoc,
        'functions' => $count,
        'ccn' => [
            'avg' => round($ccnAvg, 1),
            'p95' => $ccnP95,
        ],
        'mi' => [
            'avg' => round($avgMi, 1),
            'p5' => $miP5,
        ],
        'antipatterns' => round($antipatternsDensity, 1),
    ];
}

/**
 * Get top N hotspots sorted by CCN.
 */
function getHotspots(array $functions, int $limit = 50): array
{
    usort($functions, fn($a, $b) => $b['ccn'] - $a['ccn']);
    $hotspots = array_slice($functions, 0, $limit);

    // Return only the fields needed for output
    return array_map(fn($f) => [
        'name' => $f['name'],
        'file' => $f['file'],
        'ccn' => $f['ccn'],
        'loc' => $f['loc'],
        'mi' => $f['mi'],
        'antipatterns' => $f['antipatterns'],
    ], $hotspots);
}

// Add implicit hooks that can't be detected via AST analysis
$surfaceArea->addImplicitHooks();

// Filter specific hook instances covered by generic patterns (e.g., node_insert → entity_insert)
$surfaceArea->filterSpecificHookInstances();

// Filter global functions to remove hook implementations (must run after all hooks are known)
$surfaceArea->filterHookImplementations();

// Totals for commit analysis (sum-based metrics are always meaningful)
$ccnSum = array_sum(array_column($functions, 'ccn'));
$miDebtSum = array_sum(array_map(fn($f) => 100 - $f['mi'], $functions));

// Output JSON
$aggregates = calculateAggregates($functions, $productionLoc);
$output = [
    'production' => $aggregates,
    'testLoc' => $testLoc,
    'ccnSum' => $ccnSum,
    'miDebtSum' => $miDebtSum,
    'hotspots' => getHotspots($functions),
    'surfaceArea' => $surfaceArea->getCounts(),
    'surfaceAreaLists' => $surfaceArea->getLists(),
    'antipatterns' => $antipatterns->getCounts(),
    'parseErrors' => $parseErrors,
];

echo json_encode($output, JSON_PRETTY_PRINT) . "\n";
