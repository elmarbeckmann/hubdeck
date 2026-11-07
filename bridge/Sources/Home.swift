import Foundation
import HomeKit

/// Owns the HMHomeManager, builds snapshots, applies writes and pushes events.
final class Home: NSObject, HMHomeManagerDelegate, HMHomeDelegate, HMAccessoryDelegate {
    private let manager = HMHomeManager()
    private let server: Server
    private var ready = false
    private var loading = false
    private var services: [String: HMService] = [:]
    private var scenes: [String: (HMHome, HMActionSet)] = [:]
    private var triggers: [String: HMTrigger] = [:]
    private var pending: DispatchWorkItem?

    init(server: Server) {
        self.server = server
        super.init()
        manager.delegate = self
        server.handler = { [weak self] op, reply in self?.handle(op, reply) }
    }

    // MARK: setup

    /// Sets delegates and notifications on everything. Safe to call again after changes.
    private func attach() {
        for home in manager.homes {
            home.delegate = self
            for accessory in home.accessories {
                accessory.delegate = self
                for service in accessory.services {
                    for c in service.characteristics where c.properties.contains(HMCharacteristicPropertySupportsEventNotification) {
                        c.enableNotification(true) { _ in }
                    }
                }
            }
        }
    }

    /// Reads every readable value once, then publishes the first snapshot.
    private func loadValues() {
        guard !loading else { return }
        loading = true
        let group = DispatchGroup()
        for home in manager.homes {
            for accessory in home.accessories where accessory.isReachable {
                for service in accessory.services {
                    for c in service.characteristics where c.properties.contains(HMCharacteristicPropertyReadable) {
                        group.enter()
                        c.readValue { _ in group.leave() }
                    }
                }
            }
        }
        var finished = false
        let finish = { [weak self] in
            guard !finished, let self else { return }
            finished = true
            self.ready = true
            self.server.broadcast(self.snapshot())
        }
        group.notify(queue: .main, execute: finish)
        DispatchQueue.main.asyncAfter(deadline: .now() + 10, execute: finish)  // slow accessories must not block
    }

    /// Debounced: pushes a fresh full snapshot after the structure changed.
    private func structureChanged() {
        guard ready else { return }
        attach()
        pending?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.server.broadcast(self.snapshot())
        }
        pending = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: item)
    }

    // MARK: JSON

    private func json(_ value: Any?, format: String?) -> Any {
        if let s = value as? String { return s }
        guard let n = value as? NSNumber else { return NSNull() }  // Data, TLV, arrays: skipped
        if format == HMCharacteristicMetadataFormatBool { return n.boolValue }
        return n.doubleValue.isFinite ? n : NSNull()
    }

    private func charJSON(_ c: HMCharacteristic) -> [String: Any] {
        let m = c.metadata
        return [
            "type": c.characteristicType,
            "value": json(c.value, format: m?.format),
            "format": m?.format ?? NSNull(),
            "min": m?.minimumValue ?? NSNull(),
            "max": m?.maximumValue ?? NSNull(),
            "step": m?.stepValue ?? NSNull(),
            "units": m?.units ?? NSNull(),
            "readable": c.properties.contains(HMCharacteristicPropertyReadable),
            "writable": c.properties.contains(HMCharacteristicPropertyWritable),
        ]
    }

    private func snapshot() -> [String: Any] {
        services = [:]; scenes = [:]; triggers = [:]
        let homes = manager.homes.map { home -> [String: Any] in
            var rooms = home.rooms
            let defaultRoom = home.roomForEntireHome()
            if !rooms.contains(defaultRoom), home.accessories.contains(where: { $0.room == defaultRoom }) {
                rooms.append(defaultRoom)
            }
            let accessories = home.accessories.map { a -> [String: Any] in
                let list = a.services.filter { $0.serviceType != HMServiceTypeAccessoryInformation }
                for s in list { services[s.uniqueIdentifier.uuidString] = s }
                return [
                    "id": a.uniqueIdentifier.uuidString,
                    "name": a.name,
                    "room": a.room?.uniqueIdentifier.uuidString ?? "",
                    "reachable": a.isReachable,
                    "services": list.map { s -> [String: Any] in
                        ["id": s.uniqueIdentifier.uuidString, "name": s.name, "type": s.serviceType,
                         "primary": s.isPrimaryService, "chars": s.characteristics.map(charJSON)]
                    },
                ]
            }
            let sceneList = home.actionSets.map { set -> [String: Any] in
                let id = set.uniqueIdentifier.uuidString
                scenes[id] = (home, set)
                return ["id": id, "name": set.name, "kind": sceneKind(set.actionSetType)]
            }
            let triggerList = home.triggers.map { t -> [String: Any] in
                let id = t.uniqueIdentifier.uuidString
                triggers[id] = t
                return ["id": id, "name": t.name, "enabled": t.isEnabled]
            }
            return [
                "id": home.uniqueIdentifier.uuidString, "name": home.name, "primary": home.isPrimary,
                "rooms": rooms.map { ["id": $0.uniqueIdentifier.uuidString, "name": $0.name] },
                "accessories": accessories, "scenes": sceneList, "triggers": triggerList,
            ]
        }
        return ["type": "snapshot", "homes": homes]
    }

    private func sceneKind(_ type: String) -> String {
        switch type {
        case HMActionSetTypeWakeUp: return "wakeUp"
        case HMActionSetTypeSleep: return "sleep"
        case HMActionSetTypeHomeArrival: return "arrive"
        case HMActionSetTypeHomeDeparture: return "leave"
        case HMActionSetTypeTriggerOwned: return "trigger"
        default: return "custom"
        }
    }

    // MARK: ops

    private func handle(_ op: [String: Any], _ reply: @escaping ([String: Any]) -> Void) {
        func done(_ error: String? = nil) {
            var m: [String: Any] = ["type": "result", "ok": error == nil]
            if let req = op["req"] { m["req"] = req }
            if let error { m["error"] = error }
            reply(m)
        }
        switch op["op"] as? String {
        case "snapshot":
            guard ready else { return done("HomeKit is not loaded yet") }
            reply(snapshot())
            done()
        case "write":
            guard let sid = op["service"] as? String, let type = op["char"] as? String,
                  let c = services[sid]?.characteristics.first(where: { $0.characteristicType == type }) else {
                return done("unknown characteristic")
            }
            guard c.properties.contains(HMCharacteristicPropertyWritable) else { return done("not writable") }
            guard let value = native(op["value"], c) else { return done("bad value") }
            c.writeValue(value) { [weak self] error in
                DispatchQueue.main.async {
                    guard let self else { return }
                    if let error { return done(error.localizedDescription) }
                    done()
                    self.server.broadcast(["type": "char", "service": sid, "char": type,
                                           "value": self.json(value, format: c.metadata?.format)])
                }
            }
        case "scene":
            guard let id = op["id"] as? String, let (home, set) = scenes[id] else { return done("unknown scene") }
            home.executeActionSet(set) { error in
                DispatchQueue.main.async { done(error?.localizedDescription) }
            }
        case "trigger":
            guard let id = op["id"] as? String, let trigger = triggers[id],
                  let enabled = op["enabled"] as? Bool else { return done("unknown trigger") }
            trigger.enable(enabled) { [weak self] error in
                DispatchQueue.main.async {
                    done(error?.localizedDescription)
                    if error == nil { self?.structureChanged() }
                }
            }
        default:
            done("unknown op")
        }
    }

    /// JSON value to the type HomeKit expects for this characteristic.
    private func native(_ value: Any?, _ c: HMCharacteristic) -> Any? {
        let format = c.metadata?.format
        if format == HMCharacteristicMetadataFormatString { return value as? String }
        guard let n = value as? NSNumber, n.doubleValue.isFinite else { return nil }
        switch format {
        case HMCharacteristicMetadataFormatBool: return n.boolValue
        case HMCharacteristicMetadataFormatFloat: return n.doubleValue
        case nil: return n
        default: return Int(n.doubleValue.rounded())
        }
    }

    // MARK: HMHomeManagerDelegate

    func homeManagerDidUpdateHomes(_ manager: HMHomeManager) {
        attach()
        if ready { structureChanged() } else { loadValues() }
    }
    func homeManager(_ manager: HMHomeManager, didAdd home: HMHome) { structureChanged() }
    func homeManager(_ manager: HMHomeManager, didRemove home: HMHome) { structureChanged() }
    func homeManagerDidUpdatePrimaryHome(_ manager: HMHomeManager) { structureChanged() }

    // MARK: HMHomeDelegate

    func homeDidUpdateName(_ home: HMHome) { structureChanged() }
    func home(_ home: HMHome, didAdd accessory: HMAccessory) { structureChanged() }
    func home(_ home: HMHome, didRemove accessory: HMAccessory) { structureChanged() }
    func home(_ home: HMHome, didAdd room: HMRoom) { structureChanged() }
    func home(_ home: HMHome, didRemove room: HMRoom) { structureChanged() }
    func home(_ home: HMHome, didUpdateNameFor room: HMRoom) { structureChanged() }
    func home(_ home: HMHome, didUpdate room: HMRoom, for accessory: HMAccessory) { structureChanged() }
    func home(_ home: HMHome, didAdd actionSet: HMActionSet) { structureChanged() }
    func home(_ home: HMHome, didRemove actionSet: HMActionSet) { structureChanged() }
    func home(_ home: HMHome, didUpdateNameFor actionSet: HMActionSet) { structureChanged() }
    func home(_ home: HMHome, didAdd trigger: HMTrigger) { structureChanged() }
    func home(_ home: HMHome, didRemove trigger: HMTrigger) { structureChanged() }
    func home(_ home: HMHome, didUpdateNameFor trigger: HMTrigger) { structureChanged() }
    func home(_ home: HMHome, didUpdate trigger: HMTrigger) { structureChanged() }
    func home(_ home: HMHome, didUnblockAccessory accessory: HMAccessory) { structureChanged() }

    // MARK: HMAccessoryDelegate

    func accessoryDidUpdateName(_ accessory: HMAccessory) { structureChanged() }
    func accessory(_ accessory: HMAccessory, didUpdateNameFor service: HMService) { structureChanged() }
    func accessoryDidUpdateServices(_ accessory: HMAccessory) { structureChanged() }

    func accessoryDidUpdateReachability(_ accessory: HMAccessory) {
        guard ready else { return }
        server.broadcast(["type": "reach", "accessory": accessory.uniqueIdentifier.uuidString,
                          "reachable": accessory.isReachable])
    }

    func accessory(_ accessory: HMAccessory, service: HMService, didUpdateValueFor characteristic: HMCharacteristic) {
        guard ready else { return }
        server.broadcast(["type": "char", "service": service.uniqueIdentifier.uuidString,
                          "char": characteristic.characteristicType,
                          "value": json(characteristic.value, format: characteristic.metadata?.format)])
    }
}
