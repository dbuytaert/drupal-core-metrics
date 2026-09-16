<?php
ini_set('memory_limit', '1024M');

/**
 * @file
 * Measures files and emits one JSON line of facts per file. A fact is what a file's syntax or
 * text says, recorded item by item under the name of what it is: each declaration and the
 * declaration containing it, each '#' string key, each \Drupal:: and ->container->get() call,
 * the nesting depth of each array, each with the function it sits in. The extractor filters by
 * syntax - only '#'-prefixed string keys, only the two service-locator spellings - and decides
 * nothing about what any of it means: which files are production code, which functions are API,
 * which of those keys count as magic, which types are precise. Those are the definitions in
 * definitions.py, applied to these facts.
 *
 * A file's facts depend on its content alone, never on its path, which is what lets facts.py
 * measure a file once however many snapshots hold it. A class name that identifies a class is
 * recorded resolved against the file's namespace and imports, so a definition compares
 * classes rather than spellings; a subscribed event key is recorded as the code writes it.
 *
 * Standard input is the output of `git cat-file --batch`; the argument names what the files
 * are: php, js, yaml or composer-lock. Any PHP warning or notice stops the run.
 *
 * Usage: git cat-file --batch < blobs | php -dshort_open_tag=1 -ddisplay_errors=stderr extract-facts.php php
 */

require_once __DIR__ . '/../vendor/autoload.php';

use PhpParser\Node;
use PhpParser\NodeFinder;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitor\NameResolver;
use PhpParser\NodeVisitorAbstract;
use PhpParser\ParserFactory;
use PhpParser\PhpVersion;

function fail(string $message): never
{
    fwrite(STDERR, "extract-facts.php: $message\n");
    exit(1);
}

// A warning means a fact came out wrong (a lock's package name that is not a string became
// "Array"), so it stops the run like any other failure.
set_error_handler(fn (int $severity, string $message, string $file, int $line) => fail("$message at $file:$line"));

/*
 * =============================================================================
 * TEXT
 * =============================================================================
 */

/**
 * The 1-based numbers of a PHP file's lines that count as code: the lines holding something other
 * than blank space and comments. PHP's own tokenizer answers it, because reading each line's first
 * characters cannot tell a comment from a string that looks like one: the class ProxyBuilder
 * generates is a heredoc opening with a docblock, and every line of it read as a comment.
 */
function phpCodeLineNumbers(string $code): array
{
    $lineNumbers = [];
    foreach (PhpToken::tokenize($code) as $position => $token) {
        if ($token->is([T_WHITESPACE, T_COMMENT, T_DOC_COMMENT])) {
            continue;
        }
        // The shebang opening core's command-line scripts is a line for the operating system, which
        // PHP passes through as text like any other line before the opening tag.
        if ($position === 0 && $token->is(T_INLINE_HTML) && str_starts_with($token->text, '#!')) {
            continue;
        }
        // A token spanning lines, a string among them, holds each line it has something on.
        foreach (explode("\n", $token->text) as $offset => $line) {
            if (trim($line) !== '') $lineNumbers[$token->line + $offset] = true;
        }
    }
    ksort($lineNumbers);
    return array_keys($lineNumbers);
}

/**
 * The 1-based numbers of a JavaScript file's lines that count as code: not blank, not inside a
 * comment. A line starting with # is a private class field rather than a comment, as it is in PHP.
 */
function javascriptCodeLineNumbers(string $code): array
{
    $lineNumbers = [];
    $inBlockComment = false;
    foreach (explode("\n", $code) as $index => $line) {
        $trimmed = trim($line);
        if ($trimmed === '') continue;
        if ($inBlockComment) {
            if (str_contains($trimmed, '*/')) $inBlockComment = false;
            continue;
        }
        if (str_starts_with($trimmed, '//')) continue;
        if (str_starts_with($trimmed, '/*')) {
            if (!str_contains($trimmed, '*/')) $inBlockComment = true;
            continue;
        }
        if (str_starts_with($trimmed, '*')) continue;
        $lineNumbers[] = $index + 1;
    }
    return $lineNumbers;
}

/** Sorted line numbers as [first, last] runs of consecutive lines. */
function runs(array $lineNumbers): array
{
    $runs = [];
    foreach ($lineNumbers as $line) {
        if ($runs && $runs[count($runs) - 1][1] === $line - 1) {
            $runs[count($runs) - 1][1] = $line;
        } else {
            $runs[] = [$line, $line];
        }
    }
    return $runs;
}

/**
 * The comments that open a PHP file, before its first token of code: where a generator
 * writes that it generated the file.
 */
function openingComments(string $code): string
{
    $comments = '';
    foreach (PhpToken::tokenize($code) as $token) {
        if (!$token->is([T_OPEN_TAG, T_WHITESPACE, T_COMMENT, T_DOC_COMMENT])) {
            break;
        }
        $comments .= $token->text;
    }
    return $comments;
}

/** The functions a file's text declares at the start of a line, whether or not it parses as PHP. */
function textFunctions(string $code): array
{
    preg_match_all('/^function (\w+)\s*\(/m', $code, $matches);
    return $matches[1];
}

/*
 * =============================================================================
 * SYNTAX
 * =============================================================================
 */

/** Whether a file declares nothing and calls nothing, only assigning or returning values. */
function literalOnly(array $ast): bool
{
    $code = (new NodeFinder())->findFirst($ast, fn (Node $node) => $node instanceof Node\FunctionLike
        || $node instanceof Node\Stmt\ClassLike
        || $node instanceof Node\Expr\CallLike);
    if ($code !== null) {
        return false;
    }
    foreach ($ast as $statement) {
        if (!($statement instanceof Node\Stmt\Return_
            || $statement instanceof Node\Stmt\Nop
            || $statement instanceof Node\Stmt\Declare_
            || ($statement instanceof Node\Stmt\Expression && $statement->expr instanceof Node\Expr\Assign))) {
            return false;
        }
    }
    return true;
}

/** A class member's visibility: public unless it says private or protected (PHP 4's "var" is public). */
function visibility(Node\Stmt\ClassMethod|Node\Stmt\Property|Node\Stmt\ClassConst $member): string
{
    return $member->isPrivate() ? 'private' : ($member->isProtected() ? 'protected' : 'public');
}

/** A class name resolved against the file's namespace and imports; self, static and parent stay. */
function className(Node\Name $name): string
{
    return ($name->getAttribute('resolvedName') ?? $name)->toString();
}

/**
 * The types a declaration names, or null when it names none: built-in types lowercased, class
 * names resolved, an intersection as one entry (A&B), and null when the type allows null.
 */
function typeList(?Node $type): ?array
{
    return match (true) {
        $type === null => null,
        $type instanceof Node\NullableType => [...typeList($type->type), 'null'],
        $type instanceof Node\UnionType => array_merge(...array_map('typeList', $type->types)),
        $type instanceof Node\IntersectionType => [implode('&', array_merge(...array_map('typeList', $type->types)))],
        $type instanceof Node\Identifier => [strtolower($type->name)],
        $type instanceof Node\Name => [className($type)],
    };
}

/**
 * Whether a node branches on a value. A switch's default and a match's default arm are where a value
 * falls through when no branch took it, as an else is for an if, so neither is a decision; both
 * spellings of null-coalescing, ?? and ??=, branch on null.
 */
/** The logical operators that branch: both spellings of and, both of or. */
function isLogicalOperator(Node $node): bool
{
    return $node instanceof Node\Expr\BinaryOp\BooleanAnd
        || $node instanceof Node\Expr\BinaryOp\BooleanOr
        || $node instanceof Node\Expr\BinaryOp\LogicalAnd
        || $node instanceof Node\Expr\BinaryOp\LogicalOr;
}

function isDecisionPoint(Node $node): bool
{
    return $node instanceof Node\Stmt\If_
        || $node instanceof Node\Stmt\ElseIf_
        || $node instanceof Node\Stmt\While_
        || $node instanceof Node\Stmt\For_
        || $node instanceof Node\Stmt\Foreach_
        || $node instanceof Node\Stmt\Do_
        || $node instanceof Node\Stmt\Catch_
        || ($node instanceof Node\Stmt\Case_ && $node->cond !== null)
        || ($node instanceof Node\MatchArm && $node->conds !== null)
        || $node instanceof Node\Expr\Ternary
        || isLogicalOperator($node)
        || $node instanceof Node\Expr\BinaryOp\Coalesce
        || $node instanceof Node\Expr\AssignOp\Coalesce;
}

/**
 * Cyclomatic complexity: 1 plus each decision point in the function, including closures
 * it defines and excluding named functions and methods declared inside it, which are
 * measured as their own declarations.
 */
function cyclomaticComplexity(Node\Stmt\Function_|Node\Stmt\ClassMethod $function): int
{
    $points = 1;
    $walk = function (Node $node) use (&$walk, &$points): void {
        if (isDecisionPoint($node)) {
            $points++;
        }
        foreach ($node->getSubNodeNames() as $name) {
            foreach (is_array($node->$name) ? $node->$name : [$node->$name] as $child) {
                if ($child instanceof Node
                    && !$child instanceof Node\Stmt\Function_
                    && !$child instanceof Node\Stmt\ClassMethod) {
                    $walk($child);
                }
            }
        }
    };
    $walk($function);
    return $points;
}

/**
 * Cognitive complexity (SonarSource's white paper, 2017): how hard a function is to
 * read. Where cyclomatic complexity counts paths, this penalizes nesting and forgives
 * shorthand that reads linearly.
 *
 *   B1 (structural, +1 each): if, each elseif however it is spelled, else, ternary,
 *      switch, match, for, foreach, while, do-while, catch, each sequence of like logical
 *      operators, each break or continue leaving more than one loop, and a declaration
 *      that calls itself, once however many times it does.
 *   B2 (nesting, + current depth): if, ternary, switch, match, the loops and catch.
 *   Nesting deepens inside branch bodies, loops, switch and match arms, catch bodies
 *   and closures; try and finally do not deepen it.
 *
 * Named functions declared inside a function are measured as their own declarations. The
 * paper's other jump, goto, is left out: only the libraries Core vendors have used it.
 */
final class CognitiveComplexity
{
    private bool $callsItself = false;

    /** @param \PhpParser\Token[] $tokens The file's tokens, which tell an else if from an else holding an if. */
    public static function of(Node\Stmt\Function_|Node\Stmt\ClassMethod $function, array $tokens): int
    {
        $complexity = new self($function, $tokens);
        return $complexity->score($function->stmts ?? [], 0) + ($complexity->callsItself ? 1 : 0);
    }

    private function __construct(
        private Node\Stmt\Function_|Node\Stmt\ClassMethod $declaration,
        private array $tokens,
    ) {}

    private function score(array $nodes, int $nesting): int
    {
        $total = 0;
        foreach ($nodes as $node) {
            $total += $this->walk($node, $nesting);
        }
        return $total;
    }

    private function walk($node, int $nesting): int
    {
        if (!$node instanceof Node || $node instanceof Node\Stmt\Function_ || $node instanceof Node\Stmt\ClassMethod) {
            return 0;
        }
        if ($node instanceof Node\Stmt\If_) {
            return $nesting + $this->ifChain($node, $nesting);
        }
        if (($node instanceof Node\Stmt\Break_ || $node instanceof Node\Stmt\Continue_)
            && $node->num instanceof Node\Scalar\Int_ && $node->num->value > 1) {
            return 1;
        }
        if ($node instanceof Node\Stmt\While_ || $node instanceof Node\Stmt\Do_) {
            return 1 + $nesting + $this->walk($node->cond, $nesting) + $this->score($node->stmts, $nesting + 1);
        }
        if ($node instanceof Node\Stmt\Foreach_) {
            return 1 + $nesting + $this->walk($node->expr, $nesting) + $this->score($node->stmts, $nesting + 1);
        }
        if ($node instanceof Node\Stmt\For_) {
            $total = 1 + $nesting;
            foreach (array_merge($node->init, $node->cond, $node->loop) as $expression) {
                $total += $this->walk($expression, $nesting);
            }
            return $total + $this->score($node->stmts, $nesting + 1);
        }
        if ($node instanceof Node\Stmt\Switch_) {
            $total = 1 + $nesting + $this->walk($node->cond, $nesting);
            foreach ($node->cases as $case) {
                $total += $this->walk($case->cond, $nesting) + $this->score($case->stmts, $nesting + 1);
            }
            return $total;
        }
        if ($node instanceof Node\Expr\Match_) {
            $total = 1 + $nesting + $this->walk($node->cond, $nesting);
            foreach ($node->arms as $arm) {
                foreach ($arm->conds ?? [] as $condition) {
                    $total += $this->walk($condition, $nesting);
                }
                $total += $this->walk($arm->body, $nesting + 1);
            }
            return $total;
        }
        if ($node instanceof Node\Stmt\TryCatch) {
            $total = $this->score($node->stmts, $nesting);
            foreach ($node->catches as $catch) {
                $total += 1 + $nesting + $this->score($catch->stmts, $nesting + 1);
            }
            if ($node->finally !== null) {
                $total += $this->score($node->finally->stmts, $nesting);
            }
            return $total;
        }
        if ($node instanceof Node\Expr\Ternary) {
            return 1 + $nesting + $this->walk($node->cond, $nesting)
                + $this->walk($node->if, $nesting + 1) + $this->walk($node->else, $nesting + 1);
        }
        if (isLogicalOperator($node)) {
            $operators = [];
            $operands = [];
            self::flattenLogical($node, $operators, $operands);
            $total = 0;
            $previous = null;
            foreach ($operators as $operator) {
                $total += $operator !== $previous ? 1 : 0;
                $previous = $operator;
            }
            foreach ($operands as $operand) {
                $total += $this->walk($operand, $nesting);
            }
            return $total;
        }
        if ($node instanceof Node\Expr\Closure) {
            return $this->score($node->stmts, $nesting + 1);
        }
        if ($node instanceof Node\Expr\ArrowFunction) {
            return $this->walk($node->expr, $nesting + 1);
        }
        $this->callsItself = $this->callsItself || $this->isRecursiveCall($node);
        $total = 0;
        foreach ($node->getSubNodeNames() as $name) {
            foreach (is_array($node->$name) ? $node->$name : [$node->$name] as $child) {
                $total += $this->walk($child, $nesting);
            }
        }
        return $total;
    }

    /** A chain of logical operators as its operators in reading order and its other operands. */
    private static function flattenLogical(Node $node, array &$operators, array &$operands): void
    {
        if (isLogicalOperator($node)) {
            self::flattenLogical($node->left, $operators, $operands);
            $operators[] = $node instanceof Node\Expr\BinaryOp\BooleanAnd || $node instanceof Node\Expr\BinaryOp\LogicalAnd
                ? 'and' : 'or';
            self::flattenLogical($node->right, $operators, $operands);
        } else {
            $operands[] = $node;
        }
    }

    /**
     * An if with the clauses continuing it, each +1 and each sitting at the nesting the if sits at:
     * the cost of following the chain is paid once, on reading the if.
     */
    private function ifChain(Node\Stmt\If_ $if, int $nesting): int
    {
        $total = 1 + $this->walk($if->cond, $nesting) + $this->score($if->stmts, $nesting + 1);
        foreach ($if->elseifs as $elseif) {
            $total += 1 + $this->walk($elseif->cond, $nesting) + $this->score($elseif->stmts, $nesting + 1);
        }
        if ($if->else !== null) {
            $total += $this->isElseIf($if->else)
                ? $this->ifChain($if->else->stmts[0], $nesting)
                : 1 + $this->score($if->else->stmts, $nesting + 1);
        }
        return $total;
    }

    /**
     * Whether an else is the two-word "else if", which continues the chain. PHP parses it into the
     * tree it also gives `else { if ... }`, a nested if, so only the token after the else tells them
     * apart.
     */
    private function isElseIf(Node\Stmt\Else_ $else): bool
    {
        $position = $else->getStartTokenPos() + 1;
        while ($this->tokens[$position]->isIgnorable()) {
            $position++;
        }
        return $this->tokens[$position]->is(T_IF);
    }

    /**
     * Whether a call reaches the declaration being scored: a function only by its name, a method only
     * through $this->, self:: or static::. A method calling the global function of its name, as
     * FileSystem::chmod() calls chmod(), calls something else.
     */
    private function isRecursiveCall(Node $node): bool
    {
        $reachesDeclaration = $this->declaration instanceof Node\Stmt\Function_
            ? $node instanceof Node\Expr\FuncCall && $node->name instanceof Node\Name
            : ($node instanceof Node\Expr\MethodCall && $node->var instanceof Node\Expr\Variable && $node->var->name === 'this')
                || ($node instanceof Node\Expr\StaticCall && $node->class instanceof Node\Name
                    && in_array(strtolower($node->class->toString()), ['self', 'static'], true));
        return $reachesDeclaration && ($node->name instanceof Node\Name || $node->name instanceof Node\Identifier)
            && strcasecmp($node->name->toString(), $this->declaration->name->toString()) === 0;
    }
}

/**
 * LCOM4 (as defined in 1995): the number of connected components of a class's
 * method graph, or null for a class-like with no method bodies. Nodes are the methods
 * with a body. Two methods are linked when they touch a common instance property
 * ($this->x, or a promoted constructor parameter) or one calls the other ($this->m(),
 * self::m(), static::m(), and new self/new static as a call to __construct). A method's
 * accesses include the closures it defines, which bind $this, and exclude nested classes
 * and named functions, whose $this is another or none. Nullsafe access counts as access.
 */
final class Cohesion
{
    public static function lcom4(Node\Stmt\ClassLike $class): ?int
    {
        $methods = array_values(array_filter($class->getMethods(), fn (Node\Stmt\ClassMethod $method) => $method->stmts !== null));
        if (!$methods) {
            return null;
        }
        $index = [];
        foreach ($methods as $position => $method) {
            $index[strtolower($method->name->toString())] = $position;
        }
        $parent = range(0, count($methods) - 1);
        $properties = [];
        foreach ($methods as $position => $method) {
            $touched = [];
            $calls = [];
            self::collect($method, $touched, $calls);
            $properties[$position] = $touched;
            foreach (array_keys($calls) as $name) {
                if (isset($index[$name])) {
                    self::union($parent, $position, $index[$name]);
                }
            }
        }
        for ($first = 0; $first < count($methods); $first++) {
            for ($second = $first + 1; $second < count($methods); $second++) {
                if (array_intersect_key($properties[$first], $properties[$second])) {
                    self::union($parent, $first, $second);
                }
            }
        }
        $roots = [];
        foreach (array_keys($methods) as $position) {
            $roots[self::find($parent, $position)] = true;
        }
        return count($roots);
    }

    private static function collect(Node $node, array &$properties, array &$calls): void
    {
        $onThis = fn (Node $expression) => $expression instanceof Node\Expr\Variable && $expression->name === 'this';
        if (($node instanceof Node\Expr\PropertyFetch || $node instanceof Node\Expr\NullsafePropertyFetch)
            && $onThis($node->var) && $node->name instanceof Node\Identifier) {
            $properties[$node->name->toString()] = true;
        } elseif (($node instanceof Node\Expr\MethodCall || $node instanceof Node\Expr\NullsafeMethodCall)
            && $onThis($node->var) && $node->name instanceof Node\Identifier) {
            $calls[strtolower($node->name->toString())] = true;
        } elseif ($node instanceof Node\Expr\StaticCall && $node->class instanceof Node\Name
            && in_array(strtolower($node->class->toString()), ['self', 'static'], true)
            && $node->name instanceof Node\Identifier) {
            $calls[strtolower($node->name->toString())] = true;
        } elseif ($node instanceof Node\Expr\New_ && $node->class instanceof Node\Name
            && in_array(strtolower($node->class->toString()), ['self', 'static'], true)) {
            $calls['__construct'] = true;
        } elseif ($node instanceof Node\Param && $node->flags !== 0
            && $node->var instanceof Node\Expr\Variable && is_string($node->var->name)) {
            $properties[$node->var->name] = true;
        }
        foreach ($node->getSubNodeNames() as $name) {
            foreach (is_array($node->$name) ? $node->$name : [$node->$name] as $child) {
                if ($child instanceof Node && !$child instanceof Node\Stmt\ClassLike && !$child instanceof Node\Stmt\Function_) {
                    self::collect($child, $properties, $calls);
                }
            }
        }
    }

    private static function find(array &$parent, int $node): int
    {
        while ($parent[$node] !== $node) {
            $parent[$node] = $parent[$parent[$node]];
            $node = $parent[$node];
        }
        return $node;
    }

    private static function union(array &$parent, int $first, int $second): void
    {
        $parent[self::find($parent, $first)] = self::find($parent, $second);
    }
}

/**
 * One pass over a file's syntax tree, recording each declaration with the declaration that
 * contains it, and, with the innermost function or method containing it, each '#' string key,
 * each service locator call and each array's nesting depth.
 */
final class FactCollector extends NodeVisitorAbstract
{
    public array $declarations = [];
    public array $stringKeys = [];
    public array $serviceLocatorCalls = [];
    public array $subscribedEventKeys = [];
    /** @var array<string, array> Occurrences of each array depth in each function. */
    public array $arrayDepths = [];
    /** @var array<array{int, int}> The line ranges of class-likes and of functions. */
    public array $classLikeRanges = [];
    public array $functionRanges = [];

    /** @var int[] Ids of the declarations enclosing the node being visited. */
    private array $declarationStack = [];
    /** @var Node[] The declaration nodes those ids stand for, to pop them on leaving. */
    private array $declarationNodes = [];
    /** @var int[] Ids of the functions and methods enclosing the node being visited. */
    private array $functionStack = [];
    private int $arrayLiteralDepth = 0;

    /** @param \PhpParser\Token[] $tokens The file's tokens, which cognitive complexity reads. */
    public function __construct(private array $tokens) {}

    public function enterNode(Node $node)
    {
        $function = $this->functionStack ? end($this->functionStack) : null;

        if ($node instanceof Node\Stmt\ClassLike) {
            $this->classLikeRanges[] = [$node->getStartLine(), $node->getEndLine()];
            $this->push($node, $this->declare($node, match (true) {
                $node instanceof Node\Stmt\Class_ => 'class',
                $node instanceof Node\Stmt\Interface_ => 'interface',
                $node instanceof Node\Stmt\Trait_ => 'trait',
                $node instanceof Node\Stmt\Enum_ => 'enum',
            }, $node->name?->toString(), ['lcom4' => Cohesion::lcom4($node),
                'resolvedName' => $node->namespacedName?->toString()] + match (true) {
                $node instanceof Node\Stmt\Class_ => [
                    'extends' => $node->extends ? [className($node->extends)] : [],
                    'implements' => array_map('className', $node->implements),
                ],
                $node instanceof Node\Stmt\Interface_ => ['extends' => array_map('className', $node->extends)],
                $node instanceof Node\Stmt\Enum_ => ['implements' => array_map('className', $node->implements)],
                default => [],
            }));
        } elseif ($node instanceof Node\Stmt\Function_ || $node instanceof Node\Stmt\ClassMethod) {
            if ($node instanceof Node\Stmt\Function_) {
                $this->functionRanges[] = [$node->getStartLine(), $node->getEndLine()];
            }
            $id = $this->declare($node, $node instanceof Node\Stmt\Function_ ? 'function' : 'method', $node->name->toString(), [
                'hasBody' => $node->stmts !== null,
                'parameterTypes' => array_map(fn (Node\Param $parameter) => typeList($parameter->type), $node->params),
                'returnType' => typeList($node->returnType),
                'cognitive' => CognitiveComplexity::of($node, $this->tokens),
                'cyclomatic' => cyclomaticComplexity($node),
            ] + ($node instanceof Node\Stmt\ClassMethod ? ['visibility' => visibility($node)] : []));
            $this->push($node, $id);
            $this->functionStack[] = $id;
            if ($node instanceof Node\Stmt\ClassMethod && strcasecmp($node->name->toString(), 'getSubscribedEvents') === 0) {
                $this->collectSubscribedEventKeys($node->stmts ?? [], $id);
            }
        } elseif ($node instanceof Node\Stmt\Property) {
            $this->declare($node, 'property', implode(',', array_map(fn ($property) => $property->name->toString(), $node->props)),
                ['visibility' => visibility($node)]);
        } elseif ($node instanceof Node\Stmt\ClassConst) {
            $this->declare($node, 'class_constant', implode(',', array_map(fn ($constant) => $constant->name->toString(), $node->consts)),
                ['visibility' => visibility($node)]);
        } elseif ($node instanceof Node\Stmt\Const_) {
            $this->declare($node, 'constant', implode(',', array_map(fn ($constant) => $constant->name->toString(), $node->consts)));
        }

        if ($node instanceof Node\Expr\ArrayDimFetch) {
            if ($node->dim instanceof Node\Scalar\String_ && str_starts_with($node->dim->value, '#')) {
                $this->stringKeys[] = ['function' => $function, 'syntax' => 'access', 'name' => $node->dim->value,
                    'line' => $node->getStartLine()];
            }
            // A chain of accesses, $a['x']['y']['z'], is one access as deep as the chain,
            // recorded at its outermost node.
            if (!$node->getAttribute('insideAccessChain')) {
                $depth = 1;
                for ($inner = $node->var; $inner instanceof Node\Expr\ArrayDimFetch; $inner = $inner->var) {
                    $inner->setAttribute('insideAccessChain', true);
                    $depth++;
                }
                $this->countDepth($function, $depth);
            }
        } elseif ($node instanceof Node\ArrayItem
            && $node->key instanceof Node\Scalar\String_ && str_starts_with($node->key->value, '#')) {
            $this->stringKeys[] = ['function' => $function, 'syntax' => 'literal', 'name' => $node->key->value,
                'line' => $node->getStartLine()];
        } elseif ($node instanceof Node\Expr\Array_) {
            $this->countDepth($function, ++$this->arrayLiteralDepth);
        } elseif ($node instanceof Node\Expr\StaticCall && $node->class instanceof Node\Name
            && strcasecmp(className($node->class), 'Drupal') === 0) {
            $this->serviceLocatorCalls[] = ['function' => $function];
        } elseif (($node instanceof Node\Expr\MethodCall || $node instanceof Node\Expr\NullsafeMethodCall)
            && $node->name instanceof Node\Identifier && strcasecmp($node->name->name, 'get') === 0
            && ($node->var instanceof Node\Expr\PropertyFetch || $node->var instanceof Node\Expr\NullsafePropertyFetch)
            && $node->var->name instanceof Node\Identifier && $node->var->name->name === 'container') {
            $this->serviceLocatorCalls[] = ['function' => $function];
        }
        return null;
    }

    public function leaveNode(Node $node)
    {
        if ($node instanceof Node\Expr\Array_) {
            $this->arrayLiteralDepth--;
        }
        if ($this->declarationNodes && end($this->declarationNodes) === $node) {
            array_pop($this->declarationNodes);
            array_pop($this->declarationStack);
            if ($node instanceof Node\Stmt\Function_ || $node instanceof Node\Stmt\ClassMethod) {
                array_pop($this->functionStack);
            }
        }
        return null;
    }

    private function push(Node $node, int $id): void
    {
        $this->declarationNodes[] = $node;
        $this->declarationStack[] = $id;
    }

    /**
     * Records a declaration with every field a declaration has, null or empty where its kind has
     * no such syntax (a property has no body, a function extends nothing), so every declaration
     * arrives with the same fields.
     */
    private function declare(Node $node, string $kind, ?string $name, array $properties = []): int
    {
        $id = count($this->declarations) + 1;
        $this->declarations[] = $properties + [
            'id' => $id,
            'parent' => $this->declarationStack ? end($this->declarationStack) : null,
            'kind' => $kind,
            'name' => $name,
            // A class-like also by the name that identifies it, its namespace and name, so a rule can
            // follow what a class extends into the class it names. Null for an anonymous class.
            'resolvedName' => null,
            'doc' => $node->getDocComment()?->getText(),
            'attributes' => $this->attributes($node),
            'extends' => [],
            'implements' => [],
            'lcom4' => null,
            'hasBody' => null,
            'visibility' => null,
            'parameterTypes' => [],
            'returnType' => null,
            'cognitive' => null,
            'cyclomatic' => null,
        ];
        return $id;
    }

    /** Each attribute by class, with its arguments in order: a name when named, a value when a string. */
    private function attributes(Node $node): array
    {
        $attributes = [];
        foreach ($node->attrGroups ?? [] as $group) {
            foreach ($group->attrs as $attribute) {
                $attributes[] = ['name' => className($attribute->name), 'arguments' => array_map(fn (Node\Arg $argument) => [
                    'name' => $argument->name?->toString(),
                    'value' => $argument->value instanceof Node\Scalar\String_ ? $argument->value->value : null,
                ], $attribute->args)];
            }
        }
        return $attributes;
    }

    private function countDepth(?int $function, int $depth): void
    {
        $key = "$function|$depth";
        $this->arrayDepths[$key] ??= ['function' => $function, 'depth' => $depth, 'occurrences' => 0];
        $this->arrayDepths[$key]['occurrences']++;
    }

    /**
     * The keys of every array accessed or written in a getSubscribedEvents() method: a class
     * constant as written (KernelEvents::REQUEST) or a string ('kernel.request').
     */
    private function collectSubscribedEventKeys(array $nodes, int $method): void
    {
        foreach ($nodes as $node) {
            if (!$node instanceof Node) {
                continue;
            }
            $key = match (true) {
                $node instanceof Node\Expr\ArrayDimFetch => $node->dim,
                $node instanceof Node\ArrayItem => $node->key,
                default => null,
            };
            if ($key instanceof Node\Expr\ClassConstFetch && $key->class instanceof Node\Name
                && $key->name instanceof Node\Identifier && $key->name->toString() !== 'class') {
                $this->subscribedEventKeys[] = ['function' => $method, 'name' => $key->class->toString() . '::' . $key->name];
            } elseif ($key instanceof Node\Scalar\String_) {
                $this->subscribedEventKeys[] = ['function' => $method, 'name' => $key->value];
            }
            foreach ($node->getSubNodeNames() as $name) {
                $this->collectSubscribedEventKeys(is_array($node->$name) ? $node->$name : [$node->$name], $method);
            }
        }
    }
}

function phpFacts(string $code): array
{
    static $parser = null, $legacyParser = null;
    $parser ??= (new ParserFactory())->createForNewestSupportedVersion();
    // Syntax PHP 8 removed, such as $string{0} offsets, fails the newest parser; Drupal
    // wrote it until 2008.
    $legacyParser ??= (new ParserFactory())->createForVersion(PhpVersion::fromString('7.4'));

    $parse = 'newest';
    $tokens = [];
    try {
        $ast = $parser->parse($code);
        $tokens = $parser->getTokens();
    } catch (PhpParser\Error) {
        try {
            $ast = $legacyParser->parse($code);
            $tokens = $legacyParser->getTokens();
            $parse = 'legacy';
        } catch (PhpParser\Error) {
            $ast = null;
            $parse = 'failed';
        }
    }
    $collector = new FactCollector($tokens);
    if ($ast !== null) {
        // Names are resolved in a pass of their own: the collector reads some subtrees as it
        // enters their parent, before a shared traversal would have resolved them.
        (new NodeTraverser(new NameResolver(null, ['replaceNodes' => false])))->traverse($ast);
        (new NodeTraverser($collector))->traverse($ast);
    }

    // A code line inside a class-like is counted there, else inside a function, else at file level.
    $codeLines = phpCodeLineNumbers($code);
    $inside = [];
    foreach ([[$collector->functionRanges, 'function'], [$collector->classLikeRanges, 'class-like']] as [$ranges, $place]) {
        foreach ($ranges as [$start, $end]) {
            for ($line = $start; $line <= $end; $line++) {
                $inside[$line] = $place;
            }
        }
    }
    $lineCounts = array_count_values(array_map(fn (int $line) => $inside[$line] ?? 'file', $codeLines))
        + ['class-like' => 0, 'function' => 0];

    return [
        'parse' => $parse,
        'codeLineCount' => count($codeLines),
        'classLikeLineCount' => $lineCounts['class-like'],
        'functionLineCount' => $lineCounts['function'],
        'codeLineRuns' => runs($codeLines),
        'openingComments' => openingComments($code),
        'literalOnly' => $ast === null ? null : literalOnly($ast),
        'textFunctions' => textFunctions($code),
        'declarations' => $collector->declarations,
        'stringKeys' => $collector->stringKeys,
        'serviceLocatorCalls' => $collector->serviceLocatorCalls,
        'arrayDepths' => array_values($collector->arrayDepths),
        'subscribedEventKeys' => $collector->subscribedEventKeys,
    ];
}

/*
 * =============================================================================
 * OTHER FILES
 * =============================================================================
 */

/**
 * The keys at the top of a YAML file's services: section, each with the value written on its
 * line (null when its definition follows below), and the number of indented deprecated: keys,
 * which mark deprecated services and libraries.
 */
function yamlFacts(string $content): array
{
    $serviceKeys = [];
    $inServices = false;
    foreach (explode("\n", $content) as $line) {
        if (preg_match('/^([a-z][a-z0-9_]*):\s*$/', $line, $header)) {
            $inServices = $header[1] === 'services';
        } elseif ($inServices && preg_match('/^  ([^\s#].*?):(?:[ \t]+(.*?))?[ \t\r]*$/', $line, $match)) {
            $serviceKeys[] = ['name' => $match[1], 'value' => ($match[2] ?? '') === '' ? null : $match[2]];
        }
    }
    return ['serviceKeys' => $serviceKeys, 'deprecatedKeyCount' => preg_match_all('/^[ \t]+deprecated:/m', $content)];
}

/**
 * The package names a composer.lock resolves, production and development, or null for
 * development in a lock written before Composer split them out.
 */
function composerLockFacts(string $content): array
{
    $lock = json_decode($content, true);
    if (!is_array($lock)) {
        return ['valid' => false, 'packages' => [], 'development' => null];
    }
    $names = fn ($packages) => array_map(
        fn ($package) => (string) (($package['name'] ?? null) ?: ($package['package'] ?? null) ?: ''),
        is_array($packages) ? $packages : []);
    return [
        'valid' => true,
        'packages' => $names($lock['packages'] ?? null),
        'development' => isset($lock['packages-dev']) ? $names($lock['packages-dev']) : null,
    ];
}

/*
 * =============================================================================
 * MAIN
 * =============================================================================
 */

$kind = $argv[1] ?? '';
if (count($argv) !== 2 || !in_array($kind, ['php', 'js', 'yaml', 'composer-lock'], true)) {
    fail('Usage: git cat-file --batch < blobs | php -dshort_open_tag=1 -ddisplay_errors=stderr extract-facts.php php|js|yaml|composer-lock');
}
// Drupal opened PHP files with "<?" until 2007; without short tags they parse as HTML.
if (!ini_get('short_open_tag')) {
    fail('Run with -dshort_open_tag=1.');
}

while (($header = fgets(STDIN)) !== false) {
    if (!preg_match('/^([0-9a-f]{40}|[0-9a-f]{64}) blob (\d+)$/', rtrim($header, "\n"), $match)) {
        fail('Expected a blob from git cat-file --batch, got: ' . trim($header));
    }
    $size = (int) $match[2];
    $content = $size > 0 ? stream_get_contents(STDIN, $size) : '';
    if (strlen($content) !== $size || fgetc(STDIN) !== "\n") {
        fail("Blob {$match[1]} ended early.");
    }
    $facts = match ($kind) {
        'php' => phpFacts($content),
        'js' => ['codeLineCount' => count(javascriptCodeLineNumbers($content))],
        'yaml' => yamlFacts($content),
        'composer-lock' => composerLockFacts($content),
    };
    echo json_encode(['blob' => $match[1]] + $facts, JSON_INVALID_UTF8_SUBSTITUTE | JSON_THROW_ON_ERROR), "\n";
}
