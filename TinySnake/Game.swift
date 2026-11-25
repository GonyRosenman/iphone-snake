struct Cell: Equatable, Hashable {
    var x: Int
    var y: Int
}

enum Direction {
    case up, down, left, right
    var offset: Cell {
        switch self {
        case .up: return Cell(x: 0, y: -1)
        case .down: return Cell(x: 0, y: 1)
        case .left: return Cell(x: -1, y: 0)
        case .right: return Cell(x: 1, y: 0)
        }
    }
}

struct Game {
    static let size = 20
    private(set) var snake = [Cell(x: 10, y: 10), Cell(x: 9, y: 10), Cell(x: 8, y: 10)]
    private(set) var food: Cell? = Cell(x: 13, y: 10)
    private(set) var direction = Direction.right
    private(set) var score = 0
    private(set) var isOver = false
    private var pending: Direction?

    mutating func turn(_ next: Direction) {
        guard pending == nil else { return }
        let a = direction.offset, b = next.offset
        guard a.x + b.x != 0 || a.y + b.y != 0 else { return }
        pending = next
    }

    mutating func step() {
        guard !isOver else { return }
        if let next = pending { direction = next }
        pending = nil
        let delta = direction.offset
        let head = Cell(x: snake[0].x + delta.x, y: snake[0].y + delta.y)
        let grows = head == food
        let body = grows ? snake : Array(snake.dropLast())
        guard (0..<Self.size).contains(head.x), (0..<Self.size).contains(head.y),
              !body.contains(head) else { isOver = true; return }
        snake.insert(head, at: 0)
        if grows {
            score += 1
            let free = (0..<Self.size).flatMap { y in
                (0..<Self.size).map { x in Cell(x: x, y: y) }
            }.filter { !snake.contains($0) }
            food = free.randomElement()
            if food == nil { isOver = true }
        } else { snake.removeLast() }
    }
}
