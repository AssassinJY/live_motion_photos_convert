import Foundation
import AVFoundation
import CoreMedia

struct Args {
    let input: URL
    let output: URL
    let stillTimeSeconds: Double
    let contentIdentifier: String?
}

func parseArgs() -> Args? {
    var input: String?
    var output: String?
    var still: Double?
    var content: String?

    var i = 1
    while i < CommandLine.arguments.count {
        let key = CommandLine.arguments[i]
        let next = (i + 1) < CommandLine.arguments.count ? CommandLine.arguments[i + 1] : nil
        switch key {
        case "--input":
            input = next
            i += 2
        case "--output":
            output = next
            i += 2
        case "--still-time-seconds":
            if let raw = next, let val = Double(raw) {
                still = val
            }
            i += 2
        case "--content-identifier":
            content = next
            i += 2
        default:
            i += 1
        }
    }

    guard
        let inputPath = input,
        let outputPath = output,
        let stillTime = still
    else {
        return nil
    }
    return Args(
        input: URL(fileURLWithPath: inputPath),
        output: URL(fileURLWithPath: outputPath),
        stillTimeSeconds: stillTime,
        contentIdentifier: content
    )
}

func makeMetadataItem(identifier: String, value: Any, dataType: String? = nil) -> AVMetadataItem {
    let item = AVMutableMetadataItem()
    item.identifier = AVMetadataIdentifier(rawValue: identifier)
    item.value = value as? NSCopying & NSObjectProtocol
    if let dataType = dataType {
        item.dataType = dataType
    }
    return item.copy() as! AVMetadataItem
}

func run(_ args: Args) throws {
    let asset = AVURLAsset(url: args.input)
    guard let videoTrack = asset.tracks(withMediaType: .video).first else {
        throw NSError(domain: "live-photo", code: 1, userInfo: [NSLocalizedDescriptionKey: "Missing video track"])
    }
    let audioTrack = asset.tracks(withMediaType: .audio).first

    if FileManager.default.fileExists(atPath: args.output.path) {
        try FileManager.default.removeItem(at: args.output)
    }

    let reader = try AVAssetReader(asset: asset)
    let writer = try AVAssetWriter(outputURL: args.output, fileType: .mov)
    writer.shouldOptimizeForNetworkUse = true

    let videoOut = AVAssetReaderTrackOutput(track: videoTrack, outputSettings: nil)
    videoOut.alwaysCopiesSampleData = false
    guard reader.canAdd(videoOut) else {
        throw NSError(domain: "live-photo", code: 2, userInfo: [NSLocalizedDescriptionKey: "Cannot add video reader output"])
    }
    reader.add(videoOut)

    let videoIn = AVAssetWriterInput(mediaType: .video, outputSettings: nil, sourceFormatHint: videoTrack.formatDescriptions.first as! CMFormatDescription)
    videoIn.expectsMediaDataInRealTime = false
    videoIn.transform = videoTrack.preferredTransform
    guard writer.canAdd(videoIn) else {
        throw NSError(domain: "live-photo", code: 3, userInfo: [NSLocalizedDescriptionKey: "Cannot add video writer input"])
    }
    writer.add(videoIn)

    var audioOut: AVAssetReaderTrackOutput?
    var audioIn: AVAssetWriterInput?
    if let track = audioTrack {
        let out = AVAssetReaderTrackOutput(track: track, outputSettings: nil)
        out.alwaysCopiesSampleData = false
        if reader.canAdd(out) {
            reader.add(out)
            audioOut = out

            let inAudio = AVAssetWriterInput(mediaType: .audio, outputSettings: nil, sourceFormatHint: track.formatDescriptions.first as! CMFormatDescription)
            inAudio.expectsMediaDataInRealTime = false
            if writer.canAdd(inAudio) {
                writer.add(inAudio)
                audioIn = inAudio
            }
        }
    }

    let spec: NSDictionary = [
        kCMMetadataFormatDescriptionMetadataSpecificationKey_Identifier as NSString: "mdta/com.apple.quicktime.still-image-time",
        kCMMetadataFormatDescriptionMetadataSpecificationKey_DataType as NSString: "com.apple.metadata.datatype.int8"
    ]
    var metaDesc: CMFormatDescription?
    let createStatus = CMMetadataFormatDescriptionCreateWithMetadataSpecifications(
        allocator: kCFAllocatorDefault,
        metadataType: kCMMetadataFormatType_Boxed,
        metadataSpecifications: [spec] as CFArray,
        formatDescriptionOut: &metaDesc
    )
    guard createStatus == noErr, let formatDesc = metaDesc else {
        throw NSError(domain: "live-photo", code: 4, userInfo: [NSLocalizedDescriptionKey: "Cannot create metadata format description"])
    }

    let metaIn = AVAssetWriterInput(mediaType: .metadata, outputSettings: nil, sourceFormatHint: formatDesc)
    metaIn.expectsMediaDataInRealTime = false
    guard writer.canAdd(metaIn) else {
        throw NSError(domain: "live-photo", code: 5, userInfo: [NSLocalizedDescriptionKey: "Cannot add metadata writer input"])
    }
    writer.add(metaIn)
    let adaptor = AVAssetWriterInputMetadataAdaptor(assetWriterInput: metaIn)

    var globalMetadata: [AVMetadataItem] = []
    if let cid = args.contentIdentifier, !cid.isEmpty {
        globalMetadata.append(
            makeMetadataItem(
                identifier: "mdta/com.apple.quicktime.content.identifier",
                value: cid as NSString
            )
        )
    }
    globalMetadata.append(
        makeMetadataItem(
            identifier: "mdta/com.apple.quicktime.live-photo.auto",
            value: 1 as NSNumber
        )
    )
    writer.metadata = globalMetadata

    guard writer.startWriting() else {
        throw writer.error ?? NSError(domain: "live-photo", code: 6, userInfo: [NSLocalizedDescriptionKey: "startWriting failed"])
    }
    guard reader.startReading() else {
        throw reader.error ?? NSError(domain: "live-photo", code: 7, userInfo: [NSLocalizedDescriptionKey: "startReading failed"])
    }
    writer.startSession(atSourceTime: .zero)

    let group = DispatchGroup()
    let queueVideo = DispatchQueue(label: "livephoto.video.queue")
    let queueAudio = DispatchQueue(label: "livephoto.audio.queue")

    group.enter()
    videoIn.requestMediaDataWhenReady(on: queueVideo) {
        while videoIn.isReadyForMoreMediaData {
            if let sample = videoOut.copyNextSampleBuffer() {
                _ = videoIn.append(sample)
            } else {
                videoIn.markAsFinished()
                group.leave()
                break
            }
        }
    }

    if let inAudio = audioIn, let outAudio = audioOut {
        group.enter()
        inAudio.requestMediaDataWhenReady(on: queueAudio) {
            while inAudio.isReadyForMoreMediaData {
                if let sample = outAudio.copyNextSampleBuffer() {
                    _ = inAudio.append(sample)
                } else {
                    inAudio.markAsFinished()
                    group.leave()
                    break
                }
            }
        }
    }

    let stillItem = AVMutableMetadataItem()
    stillItem.identifier = AVMetadataIdentifier(rawValue: "mdta/com.apple.quicktime.still-image-time")
    stillItem.dataType = "com.apple.metadata.datatype.int8"
    stillItem.value = (-1 as NSNumber)
    let ts = CMTime(seconds: max(0, args.stillTimeSeconds), preferredTimescale: 600)
    let tr = CMTimeRange(start: ts, duration: CMTime(value: 1, timescale: 600))
    let timed = AVTimedMetadataGroup(items: [stillItem.copy() as! AVMetadataItem], timeRange: tr)
    if !adaptor.append(timed) {
        throw writer.error ?? NSError(domain: "live-photo", code: 8, userInfo: [NSLocalizedDescriptionKey: "append timed metadata failed"])
    }
    metaIn.markAsFinished()

    group.wait()

    var finishError: Error?
    let sem = DispatchSemaphore(value: 0)
    writer.finishWriting {
        finishError = writer.error
        sem.signal()
    }
    sem.wait()

    if reader.status == .failed {
        throw reader.error ?? NSError(domain: "live-photo", code: 9, userInfo: [NSLocalizedDescriptionKey: "reader failed"])
    }
    if writer.status == .failed || writer.status == .cancelled {
        throw finishError ?? NSError(domain: "live-photo", code: 10, userInfo: [NSLocalizedDescriptionKey: "writer failed"])
    }
}

guard let args = parseArgs() else {
    fputs("Usage: write_live_photo_mov.swift --input <mp4> --output <mov> --still-time-seconds <sec> [--content-identifier <uuid>]\n", stderr)
    exit(2)
}

do {
    try run(args)
    exit(0)
} catch {
    fputs("ERROR: \(error.localizedDescription)\n", stderr)
    exit(1)
}
