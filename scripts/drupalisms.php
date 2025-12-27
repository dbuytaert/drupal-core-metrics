#!/usr/bin/env php
<?php
/**
 * @file
 * Analyze Drupal codebase for anti-patterns and concepts to learn.
 *
 * Two separate metrics:
 *
 * 1. ANTI-PATTERNS (per-file, density metric - should decrease)
 *    - Service Locator: Static \Drupal:: calls that bypass dependency injection
 *    - Global State: drupal_static() hidden mutable state
 *    - Deep Nesting: Complex nested array structures
 *
 * 2. CONCEPTS TO LEARN (codebase-level, count metric - total learning burden)
 *    - Plugin types: Distinct plugin systems (Block, Field, Action, etc.)
 *    - Hooks: Distinct hook names (form_alter, entity_presave, etc.)
 *    - Magic keys: Distinct #-prefixed render array keys
 *    - Events: Distinct Symfony events subscribed to
 *    - Attributes: Distinct PHP attributes for plugins and hooks
 *
 * Usage: php drupalisms.php /path/to/drupal/core
 * Output: JSON with anti-patterns, concepts, and file-level metrics
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
const GLOBAL_STATE_WEIGHT = 2;
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
 * Adapter for anti-pattern scoring.
 */
class AntipatternScoreAdapter
{
    private FileMetricsTracker $metrics;

    public function __construct(FileMetricsTracker $metrics)
    {
        $this->metrics = $metrics;
    }

    public function addScore(string $file, int $score, bool $isTest): void
    {
        if (!$isTest) {
            $this->metrics->addAntipatterns($file, $score);
        }
    }
}

/*
 * =============================================================================
 * CONCEPTS COLLECTOR
 * =============================================================================
 */

/**
 * Collects distinct concept types across the codebase.
 */
class ConceptsCollector
{
    public array $pluginTypes = [];
    public array $hooks = [];
    public array $magicKeys = [];
    public array $events = [];
    public array $attributes = [];

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

    public function addAttribute(string $attribute): void
    {
        $this->attributes[$attribute] = true;
    }

    public function getCounts(): array
    {
        return [
            'pluginTypes' => count($this->pluginTypes),
            'hooks' => count($this->hooks),
            'magicKeys' => count($this->magicKeys),
            'events' => count($this->events),
            'attributes' => count($this->attributes),
        ];
    }

    public function getLists(): array
    {
        return [
            'pluginTypes' => array_keys($this->pluginTypes),
            'hooks' => array_keys($this->hooks),
            'magicKeys' => array_keys($this->magicKeys),
            'events' => array_keys($this->events),
            'attributes' => array_keys($this->attributes),
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
            && !str_contains($file->getPathname(), '/vendor/')) {
            $files[] = $file->getPathname();
        }
    }
    return $files;
}

/**
 * Extract hook pattern from function name.
 * e.g., "mymodule_form_alter" -> "hook_form_alter"
 *       "mymodule_preprocess_node" -> "hook_preprocess_THEME"
 */
function extractHookPattern(string $functionName): ?string
{
    // Alter hooks: *_alter
    if (str_ends_with($functionName, '_alter')) {
        // Extract the hook type (e.g., form_alter, entity_view_alter)
        if (preg_match('/^[a-z_]+?_([a-z_]+_alter)$/', $functionName, $m)) {
            return 'hook_' . $m[1];
        }
        return 'hook_alter';
    }

    // Preprocess: *_preprocess_*
    if (preg_match('/_preprocess_([a-z_]+)$/', $functionName, $m)) {
        return 'hook_preprocess_THEME';
    }

    // Process: *_process_*
    if (preg_match('/_process_([a-z_]+)$/', $functionName, $m)) {
        return 'hook_process_THEME';
    }

    return null;
}

/*
 * =============================================================================
 * AST VISITORS - ANTI-PATTERNS (contribute to per-file score)
 * =============================================================================
 */

/**
 * GLOBAL STATE - drupal_static() calls (anti-pattern)
 */
class GlobalStateVisitor extends NodeVisitorAbstract
{
    private AntipatternScoreAdapter $tracker;
    private string $currentFile = '';
    private bool $isTestFile = false;

    public function __construct(AntipatternScoreAdapter $tracker)
    {
        $this->tracker = $tracker;
    }

    public function setCurrentFile(string $file, bool $isTest): void
    {
        $this->currentFile = $file;
        $this->isTestFile = $isTest;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Expr\FuncCall)
            || !($node->name instanceof Node\Name)) {
            return null;
        }

        $name = $node->name->toString();
        if ($name === 'drupal_static' || $name === 'drupal_static_reset') {
            if (!$this->isTestFile) {
                $this->tracker->addScore($this->currentFile, GLOBAL_STATE_WEIGHT, false);
            }
        }
        return null;
    }
}

/**
 * SERVICE LOCATOR - \Drupal:: static calls (anti-pattern)
 */
class ServiceLocatorVisitor extends NodeVisitorAbstract
{
    private AntipatternScoreAdapter $tracker;
    private string $currentFile = '';
    private bool $isTestFile = false;

    public function __construct(AntipatternScoreAdapter $tracker)
    {
        $this->tracker = $tracker;
    }

    public function setCurrentFile(string $file, bool $isTest): void
    {
        $this->currentFile = $file;
        $this->isTestFile = $isTest;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Expr\StaticCall)
            || !($node->class instanceof Node\Name)) {
            return null;
        }

        $className = $node->class->toString();
        if ($className === 'Drupal' || $className === '\\Drupal') {
            if (!$this->isTestFile) {
                $this->tracker->addScore($this->currentFile, SERVICE_LOCATOR_WEIGHT, false);
            }
        }
        return null;
    }
}

/**
 * DEEP NESTING - Array access (anti-pattern)
 */
class DeepArrayVisitor extends NodeVisitorAbstract
{
    private AntipatternScoreAdapter $tracker;
    private string $currentFile = '';
    private bool $isTestFile = false;

    public function __construct(AntipatternScoreAdapter $tracker)
    {
        $this->tracker = $tracker;
    }

    public function setCurrentFile(string $file, bool $isTest): void
    {
        $this->currentFile = $file;
        $this->isTestFile = $isTest;
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

        if ($depth > 2 && !$this->isTestFile) {
            $this->tracker->addScore($this->currentFile, $depth - 2, false);
        }

        return NodeTraverser::DONT_TRAVERSE_CHILDREN;
    }
}

/**
 * DEEP NESTING - Array literals (anti-pattern)
 */
class DeepArrayLiteralVisitor extends NodeVisitorAbstract
{
    private AntipatternScoreAdapter $tracker;
    private string $currentFile = '';
    private bool $isTestFile = false;
    private int $currentDepth = 0;

    public function __construct(AntipatternScoreAdapter $tracker)
    {
        $this->tracker = $tracker;
    }

    public function setCurrentFile(string $file, bool $isTest): void
    {
        $this->currentFile = $file;
        $this->isTestFile = $isTest;
        $this->currentDepth = 0;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Expr\Array_)) {
            return null;
        }

        $this->currentDepth++;
        if ($this->currentDepth > 2 && !$this->isTestFile) {
            $this->tracker->addScore($this->currentFile, $this->currentDepth - 2, false);
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
 * AST VISITORS - CONCEPTS (collect distinct types)
 * =============================================================================
 */

/**
 * MAGIC KEYS - Collect distinct #-prefixed keys (concept)
 */
class MagicKeyVisitor extends NodeVisitorAbstract
{
    private ConceptsCollector $concepts;

    public function __construct(ConceptsCollector $concepts)
    {
        $this->concepts = $concepts;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Expr\ArrayItem)
            || !($node->key instanceof Node\Scalar\String_)
            || !str_starts_with($node->key->value, '#')) {
            return null;
        }

        $key = $node->key->value;
        if (in_array($key, IGNORED_KEYS)) {
            return null;
        }

        // Skip color values like #000, #fff, #aabbcc
        if (preg_match('/^#[0-9a-fA-F]{3,6}$/', $key)) {
            return null;
        }

        // Skip keys that are just # or very short
        if (strlen($key) < 3) {
            return null;
        }

        // Track all #-prefixed keys as concepts to learn
        $this->concepts->addMagicKey($key);
        return null;
    }
}

/**
 * HOOKS - Collect distinct hook patterns (concept)
 *
 * Detects both:
 * - Procedural hooks: function mymodule_form_alter() {}
 * - OOP hooks: #[Hook('form_alter')] public function formAlter() {}
 */
class HookTypeVisitor extends NodeVisitorAbstract
{
    private ConceptsCollector $concepts;
    private string $currentFile = '';

    public function __construct(ConceptsCollector $concepts)
    {
        $this->concepts = $concepts;
    }

    public function setCurrentFile(string $file): void
    {
        $this->currentFile = $file;
    }

    public function enterNode(Node $node): ?int
    {
        // Skip .api.php files (documentation)
        if (str_ends_with($this->currentFile, '.api.php')) {
            return null;
        }

        // Procedural hooks: function mymodule_form_alter()
        if ($node instanceof Node\Stmt\Function_) {
            $name = $node->name->toString();
            // Skip hook/template definitions
            if (str_starts_with($name, 'hook_') || str_starts_with($name, 'template_')) {
                return null;
            }
            $pattern = extractHookPattern($name);
            if ($pattern) {
                $this->concepts->addHook($pattern);
            }
        }

        // OOP hooks: #[Hook('form_alter')] on class methods
        if ($node instanceof Node\Stmt\ClassMethod) {
            foreach ($node->attrGroups as $attrGroup) {
                foreach ($attrGroup->attrs as $attr) {
                    $attrName = $attr->name->toString();
                    if ($attrName === 'Hook' || str_ends_with($attrName, '\\Hook')) {
                        $hookName = $this->extractHookNameFromAttribute($attr);
                        if ($hookName) {
                            $this->concepts->addHook('hook_' . $hookName);
                        }
                    }
                }
            }
        }

        return null;
    }

    private function extractHookNameFromAttribute(Node\Attribute $attr): ?string
    {
        if (empty($attr->args)) {
            return null;
        }
        $firstArg = $attr->args[0]->value;
        if ($firstArg instanceof Node\Scalar\String_) {
            return $firstArg->value;
        }
        return null;
    }
}

/**
 * PLUGIN TYPES - Collect distinct plugin managers (concept)
 */
class PluginManagerVisitor extends NodeVisitorAbstract
{
    private ConceptsCollector $concepts;

    public function __construct(ConceptsCollector $concepts)
    {
        $this->concepts = $concepts;
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
            $this->concepts->addPluginType($className);
        }
        return null;
    }
}

/**
 * EVENTS - Collect distinct Symfony events from EventSubscriberInterface (concept)
 */
class EventSubscriberVisitor extends NodeVisitorAbstract
{
    private ConceptsCollector $concepts;
    private bool $inSubscriber = false;

    public function __construct(ConceptsCollector $concepts)
    {
        $this->concepts = $concepts;
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
        // Look for array keys in return statements (event names are array keys)
        $this->findEventNames($method->stmts ?? []);
    }

    private function findEventNames(array $stmts): void
    {
        foreach ($stmts as $stmt) {
            if ($stmt instanceof Node\Stmt\Return_ && $stmt->expr instanceof Node\Expr\Array_) {
                foreach ($stmt->expr->items as $item) {
                    if ($item && $item->key) {
                        $eventName = $this->extractEventName($item->key);
                        if ($eventName) {
                            $this->concepts->addEvent($eventName);
                        }
                    }
                }
            }
        }
    }

    private function extractEventName(Node $node): ?string
    {
        // String literal: 'kernel.request'
        if ($node instanceof Node\Scalar\String_) {
            return $node->value;
        }
        // Class constant: KernelEvents::REQUEST (but not SomeClass::class)
        if ($node instanceof Node\Expr\ClassConstFetch && $node->name instanceof Node\Identifier) {
            $constName = $node->name->toString();
            // Skip ::class constructs - those are PHP's magic class name syntax, not event constants
            if ($constName === 'class') {
                return null;
            }
            $class = $node->class instanceof Node\Name ? $node->class->toString() : '';
            return $class . '::' . $constName;
        }
        return null;
    }
}

/**
 * ATTRIBUTES - Collect distinct PHP attributes and annotations (concept)
 *
 * Detects both:
 * - Docblock annotations: @Block(...)
 * - PHP 8 attributes: #[Block(...)]
 */
class AnnotationVisitor extends NodeVisitorAbstract
{
    private ConceptsCollector $concepts;

    public function __construct(ConceptsCollector $concepts)
    {
        $this->concepts = $concepts;
    }

    public function enterNode(Node $node): ?int
    {
        if (!($node instanceof Node\Stmt\Class_)) {
            return null;
        }

        // Check docblock for @Annotation patterns (legacy)
        $docComment = $node->getDocComment();
        if ($docComment) {
            $this->extractDocblockAnnotations($docComment->getText());
        }

        // Check PHP 8 attributes #[Attribute]
        foreach ($node->attrGroups as $attrGroup) {
            foreach ($attrGroup->attrs as $attr) {
                $attrName = $attr->name->toString();
                // Get just the class name without namespace
                $shortName = str_contains($attrName, '\\')
                    ? substr($attrName, strrpos($attrName, '\\') + 1)
                    : $attrName;
                // Only count capitalized names (plugin types, not generic attributes)
                if (ctype_upper($shortName[0])) {
                    $this->concepts->addAttribute('#[' . $shortName . ']');
                }
            }
        }

        return null;
    }

    private function extractDocblockAnnotations(string $docblock): void
    {
        // Look for @AnnotationName( patterns in docblocks
        if (preg_match_all('/@([A-Z][a-zA-Z]+)\s*\(/', $docblock, $matches)) {
            foreach ($matches[1] as $annotation) {
                $this->concepts->addAttribute('@' . $annotation);
            }
        }
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
$traverser = new NodeTraverser();
$fileMetrics = new FileMetricsTracker();
$antipatternAdapter = new AntipatternScoreAdapter($fileMetrics);
$concepts = new ConceptsCollector();

// Anti-pattern visitors (contribute to per-file score)
$globalStateVisitor = new GlobalStateVisitor($antipatternAdapter);
$serviceLocatorVisitor = new ServiceLocatorVisitor($antipatternAdapter);
$deepArrayVisitor = new DeepArrayVisitor($antipatternAdapter);
$deepArrayLiteralVisitor = new DeepArrayLiteralVisitor($antipatternAdapter);

// Concept visitors (collect distinct types)
$magicKeyVisitor = new MagicKeyVisitor($concepts);
$hookTypeVisitor = new HookTypeVisitor($concepts);
$pluginManagerVisitor = new PluginManagerVisitor($concepts);
$eventSubscriberVisitor = new EventSubscriberVisitor($concepts);
$annotationVisitor = new AnnotationVisitor($concepts);

// CCN visitor
$ccnVisitor = new CcnVisitor($fileMetrics);

// Add all visitors
$traverser->addVisitor($ccnVisitor);
$traverser->addVisitor($globalStateVisitor);
$traverser->addVisitor($serviceLocatorVisitor);
$traverser->addVisitor($deepArrayVisitor);
$traverser->addVisitor($deepArrayLiteralVisitor);
$traverser->addVisitor($magicKeyVisitor);
$traverser->addVisitor($hookTypeVisitor);
$traverser->addVisitor($pluginManagerVisitor);
$traverser->addVisitor($eventSubscriberVisitor);
$traverser->addVisitor($annotationVisitor);

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
            $ccnVisitor->setCurrentFile($relativePath);
            $globalStateVisitor->setCurrentFile($relativePath, $isTest);
            $serviceLocatorVisitor->setCurrentFile($relativePath, $isTest);
            $deepArrayVisitor->setCurrentFile($relativePath, $isTest);
            $deepArrayLiteralVisitor->setCurrentFile($relativePath, $isTest);
            $hookTypeVisitor->setCurrentFile($relativePath);
            $traverser->traverse($ast);
        }

        $fileMetrics->calculateMi($relativePath);
    } catch (Exception $e) {
        $parseErrors++;
    }
}

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
    'concepts' => $concepts->getCounts(),
    'conceptLists' => $concepts->getLists(),
    'files' => $allFiles,
    'filesAnalyzed' => count($files),
    'parseErrors' => $parseErrors,
];

echo json_encode($output, JSON_PRETTY_PRINT) . "\n";
