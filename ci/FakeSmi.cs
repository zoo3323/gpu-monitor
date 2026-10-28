// 시험용 가짜 nvidia-smi. A100 두 장, GPU 를 쓰는 프로세스는 C:\fake-smi\apps.txt 에서 읽는다.
using System;
using System.IO;
using System.Linq;

class FakeSmi {
  static string Field(string f, string idx, string uuid, string util, string used, string temp) {
    switch (f) {
      case "index": return idx;
      case "name": return "NVIDIA A100-SXM4-80GB";
      case "utilization.gpu": return util;
      case "memory.used": return used;
      case "memory.total": return "81920";
      case "temperature.gpu": return temp;
      case "power.draw": return "312.5";
      case "power.limit": return "400.00";
      case "fan.speed": return "[N/A]";
      case "clocks.sm": return "1410";
      case "uuid": return uuid;
      case "pci.bus_id": return "00000000:0" + (idx == "0" ? "1" : "2") + ":00.0";
      default: return "[N/A]";
    }
  }
  static int Main(string[] args) {
    if (args.Contains("-L")) {
      Console.WriteLine("GPU 0: NVIDIA A100-SXM4-80GB (UUID: GPU-fake-0)");
      Console.WriteLine("GPU 1: NVIDIA A100-SXM4-80GB (UUID: GPU-fake-1)");
      return 0;
    }
    string q = args.FirstOrDefault(a => a.StartsWith("--query-gpu="));
    if (q != null) {
      string[] fields = q.Substring(12).Split(',');
      Console.WriteLine(string.Join(", ", fields.Select(f => Field(f, "0", "GPU-fake-0", "93", "61000", "66"))));
      Console.WriteLine(string.Join(", ", fields.Select(f => Field(f, "1", "GPU-fake-1", "71", "30000", "61"))));
      return 0;
    }
    if (args.Any(a => a.StartsWith("--query-compute-apps="))) {
      string path = @"C:\fake-smi\apps.txt";
      if (File.Exists(path)) foreach (string line in File.ReadAllLines(path)) if (line.Trim().Length > 0) Console.WriteLine(line);
      return 0;
    }
    Console.WriteLine("fake nvidia-smi");
    return 0;
  }
}
