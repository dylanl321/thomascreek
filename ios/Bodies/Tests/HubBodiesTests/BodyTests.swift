import Foundation
import XCTest
@testable import HubBodies

/// Each body the hub sends the phone, decoded with the app's model. A failure names the field that broke.
final class BodyTests: XCTestCase {
    /// The same decoder as HubClient.decoder: the hub sends snake_case.
    private let decoder: JSONDecoder = { let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase; return d }()

    /// Fixture name -> decode with the model the app uses for that route.
    private lazy var decoders: [String: (Data) throws -> Any] = [
        "home": { try self.decoder.decode(HomeSnapshot.self, from: $0) },
        "me": { try self.decoder.decode(Me.self, from: $0) },
        "smart": { try self.decoder.decode(SmartList.self, from: $0) },
        "inbox": { try self.decoder.decode(InboxList.self, from: $0) },
        "activity": { try self.decoder.decode(ActivityList.self, from: $0) },
        "search": { try self.decoder.decode(SearchResults.self, from: $0) },
        "tv_explore": { try self.decoder.decode(TVFeed.self, from: $0) },
        "tv_upnext": { try (self.decoder.decode(TVFeed.self, from: $0), self.decoder.decode(UpNextPage.self, from: $0)) },
        "tv_guide": { try self.decoder.decode(TVGuidePage.self, from: $0) },
        "tv_players": { try self.decoder.decode(TVPlayerList.self, from: $0) },
        "tv_series": { try self.decoder.decode(TVSeriesPage.self, from: $0) },
        "tv_movies": { try self.decoder.decode(TVMoviePage.self, from: $0) },
        "devices_join": { try self.decoder.decode(JoinCode.self, from: $0) },
        "kids_ratings": { try self.decoder.decode(KidRatings.self, from: $0) },
        "tv_phone_next": { try self.decoder.decode(PlayNextReply.self, from: $0) },
        "tv_phone_queue": { try self.decoder.decode(PlayQueue.self, from: $0) },
        "tv_phone_segments": { try self.decoder.decode(PlaySegments.self, from: $0) },
    ]

    private func fixtures() throws -> [URL] {
        let dir = try XCTUnwrap(Bundle.module.url(forResource: "Fixtures", withExtension: nil))
        return try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    func testEveryFixtureDecodesWithTheAppsModel() throws {
        let files = try fixtures()
        XCTAssertFalse(files.isEmpty, "no fixtures: run IOS_FIXTURES=update on the hub's test_ios_bodies.py")
        for file in files {
            let name = file.deletingPathExtension().lastPathComponent
            guard let decode = decoders[name] else {
                XCTFail("\(name).json has no decode test: add it to BodyTests.decoders")
                continue
            }
            do {
                _ = try decode(try Data(contentsOf: file))
            } catch {
                XCTFail("\(name).json doesn't decode: \(Self.explain(error))")
            }
        }
    }

    func testEveryDecodeHasItsFixture() throws {
        let names = Set(try fixtures().map { $0.deletingPathExtension().lastPathComponent })
        for name in decoders.keys where !names.contains(name) {
            XCTFail("\(name) has a decode test but no fixture")
        }
    }

    func testThePlayerBodiesKeepWhatTheScreenNeeds() throws {
        func data(_ name: String) throws -> Data {
            try Data(contentsOf: try XCTUnwrap(try fixtures().first { $0.lastPathComponent == name + ".json" }))
        }
        let next = try XCTUnwrap(try decoder.decode(PlayNextReply.self, from: data("tv_phone_next")).next)
        XCTAssertTrue(next.episodeId.hasPrefix("ep:"))
        XCTAssertEqual(next.line, "S1 E2 · Next")
        let items = try XCTUnwrap(try decoder.decode(PlayQueue.self, from: data("tv_phone_queue")).items)
        XCTAssertEqual(items.count, 2)
        XCTAssertFalse(items[0].isEpisode)
        XCTAssertEqual(items[1].line, "Cartoon Show · S1 E2")
        // The last episode of a series, and a hub that knows no sections
        XCTAssertNil(try decoder.decode(PlayNextReply.self, from: Data("{}".utf8)).next)
        let parts = try decoder.decode(PlaySegments.self, from: Data(#"{"segments":[{"kind":"intro","start_s":12.5,"end_s":70}]}"#.utf8))
        XCTAssertEqual(parts.segments?.first?.label, "Skip intro")
    }

    func testTheFeedKeepsWhatAScreenNeeds() throws {
        let data = try Data(contentsOf: try XCTUnwrap(try fixtures().first { $0.lastPathComponent == "tv_upnext.json" }))
        let page = try decoder.decode(UpNextPage.self, from: data)
        let item = try XCTUnwrap(page.rows.flatMap(\.items).first)
        XCTAssertFalse(item.title.isEmpty)
        XCTAssertNotNil(item.episodeId ?? item.movieId ?? item.channelKey)
    }

    private static func explain(_ error: Error) -> String {
        guard let e = error as? DecodingError else { return "\(error)" }
        func path(_ c: DecodingError.Context) -> String { c.codingPath.map(\.stringValue).joined(separator: ".") }
        switch e {
        case .keyNotFound(let key, let c): return "missing \(path(c)).\(key.stringValue)"
        case .typeMismatch(let type, let c): return "\(path(c)) is not \(type): \(c.debugDescription)"
        case .valueNotFound(let type, let c): return "\(path(c)) is null, expected \(type)"
        case .dataCorrupted(let c): return "\(path(c)): \(c.debugDescription)"
        @unknown default: return "\(e)"
        }
    }
}
