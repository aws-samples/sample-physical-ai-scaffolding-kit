export type ImageConfig = {
  readonly baseImage: string;
  readonly buildInstanceType: string;
  readonly diskSizeGb: number;
  readonly parameters?: Record<string, Record<string, string>>;
};

export type Configuration = {
  VpcId: string;
  DefaultInstanceType: string;
  ImageNames: string[];
};
